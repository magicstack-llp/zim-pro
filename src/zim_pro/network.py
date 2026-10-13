"""How builds and feeds reach the web: the VPN proxy, the captcha solver, and their state.

A Gluetun container gives an HTTP proxy to send requests through and a control server that
says what the connection is and can be told to stop and start it, which is how the address
is changed. FlareSolverr only needs to be asked whether it is there.
"""

import asyncio
import logging
import time
from typing import Any

import httpx

from .db import Database
from .events import EventBus

log = logging.getLogger(__name__)

STATUS_TTL = 20  # seconds a look at the services is good for
CHANGE_EVERY = 60  # at least this long between two changes of address
CHANGE_TIMEOUT = 120  # how long a new connection may take to come up
CONTROL = ("vpn_control_url", "vpn_control_key", "vpn_control_user", "vpn_control_password")
# A page that answers with the address it was asked from: how a proxy is tried.
ADDRESS_ECHO = "https://www.cloudflare.com/cdn-cgi/trace"
_IP_FIELDS = ("public_ip", "country", "region", "city", "organization", "timezone")


class Network:
    """The proxy and solver in use, what they report, and changing the VPN's address."""

    def __init__(
        self,
        db: Database,
        events: EventBus,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.db = db
        self.events = events
        self.transport = transport
        self.generation = 0  # goes up each time the address is changed
        self.changes = 0
        self.last_change: float | None = None
        self.last_change_error = ""
        self._changed_at = 0.0  # monotonic
        self._steady = asyncio.Event()
        self._steady.set()
        self._changing = asyncio.Lock()
        self._status: dict[str, Any] | None = None
        self._status_at = 0.0
        self._looking = asyncio.Lock()
        self._vpn_route = "vpn"  # older Gluetun releases call it "openvpn"

    # -- what builds are to use -----------------------------------------------------

    @property
    def proxy(self) -> str:
        on = self.db.get_setting("build_proxy_enabled")
        return str(self.db.get_setting("build_proxy")) if on else ""

    @property
    def solver(self) -> str:
        on = self.db.get_setting("captcha_solver_enabled")
        return str(self.db.get_setting("captcha_solver_url")) if on else ""

    @property
    def control(self) -> str:
        return str(self.db.get_setting("vpn_control_url")).rstrip("/")

    @property
    def can_change_ip(self) -> bool:
        return bool(self.control and self.proxy)

    @property
    def changes_ip_when_refused(self) -> bool:
        return bool(self.can_change_ip and self.db.get_setting("vpn_change_ip"))

    async def steady(self) -> None:
        """Wait out a change of address: requests sent during one would only fail."""
        await self._steady.wait()

    # -- the control server ---------------------------------------------------------

    def _client(self, timeout: float = 8, typed: dict[str, str] | None = None) -> httpx.AsyncClient:
        """A client for the control server: the saved one, or one typed and not saved yet."""
        conf = typed if typed is not None else {k: str(self.db.get_setting(k)) for k in CONTROL}
        headers, auth = {}, None
        if conf["vpn_control_key"]:
            headers["X-API-Key"] = conf["vpn_control_key"]
        elif conf["vpn_control_user"]:
            auth = httpx.BasicAuth(conf["vpn_control_user"], conf["vpn_control_password"])
        return httpx.AsyncClient(
            base_url=conf["vpn_control_url"].rstrip("/"),
            headers=headers,
            auth=auth,
            timeout=timeout,
            transport=self.transport,
        )

    @staticmethod
    def _problem(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            if code in (401, 403):
                return (
                    f"The control server refused the request (HTTP {code}). Check the API "
                    "key, or the name and password."
                )
            return f"The control server answered HTTP {code}."
        if isinstance(exc, httpx.TimeoutException):
            return "The control server did not answer in time."
        if isinstance(exc, httpx.HTTPError):
            return f"The control server cannot be reached ({exc or type(exc).__name__})."
        return str(exc) or type(exc).__name__

    async def _vpn_status(self, client: httpx.AsyncClient) -> str:
        reply = await client.get(f"/v1/{self._vpn_route}/status")
        if reply.status_code == 404 and self._vpn_route == "vpn":
            self._vpn_route = "openvpn"
            reply = await client.get("/v1/openvpn/status")
        reply.raise_for_status()
        return str(reply.json().get("status", "")).lower()

    async def _set_vpn(self, client: httpx.AsyncClient, status: str) -> None:
        reply = await client.put(f"/v1/{self._vpn_route}/status", json={"status": status})
        reply.raise_for_status()

    @staticmethod
    async def _public_ip(client: httpx.AsyncClient) -> dict[str, str]:
        reply = await client.get("/v1/publicip/ip")
        reply.raise_for_status()
        data = reply.json() if reply.content else {}
        return {key: str(data.get(key) or "") for key in _IP_FIELDS}

    async def _look_at_vpn(self) -> dict[str, Any]:
        vpn: dict[str, Any] = {
            "proxy_set": bool(self.db.get_setting("build_proxy")),
            "enabled": bool(self.db.get_setting("build_proxy_enabled")),
            "control": bool(self.control),
            "change_ip": bool(self.db.get_setting("vpn_change_ip")),
            "status": "",
            "error": "",
            "changing": not self._steady.is_set(),
            "changes": self.changes,
            "last_change": self.last_change,
            "last_change_error": self.last_change_error,
            **dict.fromkeys(_IP_FIELDS, ""),
        }
        if not self.control:
            return vpn
        try:
            async with self._client() as client:
                vpn["status"] = await self._vpn_status(client)
                if vpn["status"] == "running":
                    vpn.update(await self._public_ip(client))
        except (httpx.HTTPError, ValueError) as exc:
            vpn["error"] = self._problem(exc)
        return vpn

    async def try_vpn(self, typed: dict[str, str]) -> dict[str, Any]:
        """Try a control server and a proxy as typed: does each answer, and do they agree?

        The proxy is tried by asking a public page, through it, which address it sees.
        """
        result: dict[str, Any] = {"control": None, "proxy": None, "same_address": None}
        if typed["vpn_control_url"]:
            control: dict[str, Any] = {"ok": False, "status": "", "error": "", "ms": 0}
            began = time.monotonic()
            try:
                async with self._client(8, typed) as client:
                    control["status"] = await self._vpn_status(client)
                    control.update(await self._public_ip(client))
                control["ok"] = True
            except (httpx.HTTPError, ValueError) as exc:
                control["error"] = self._problem(exc)
            control["ms"] = round((time.monotonic() - began) * 1000)
            result["control"] = control
        if typed["build_proxy"]:
            proxy: dict[str, Any] = {"ok": False, "ip": "", "country": "", "error": "", "ms": 0}
            began = time.monotonic()
            try:
                # A test transport stands in for the proxy; httpx takes one or the other.
                route = (
                    {"transport": self.transport}
                    if self.transport
                    else {"proxy": typed["build_proxy"]}
                )
                async with httpx.AsyncClient(timeout=15, **route) as client:
                    reply = await client.get(ADDRESS_ECHO)
                    reply.raise_for_status()
                seen = dict(line.split("=", 1) for line in reply.text.splitlines() if "=" in line)
                proxy.update(ok=bool(seen.get("ip")), ip=seen.get("ip", ""))
                proxy["country"] = seen.get("loc", "")
                if not proxy["ok"]:
                    proxy["error"] = "A page came back through it, but not the one asked for."
            except httpx.HTTPStatusError as exc:
                proxy["error"] = f"The test page answered HTTP {exc.response.status_code}."
            except (httpx.HTTPError, ValueError) as exc:
                proxy["error"] = (
                    f"Nothing could be fetched through it ({exc or type(exc).__name__})."
                )
            proxy["ms"] = round((time.monotonic() - began) * 1000)
            result["proxy"] = proxy
        control, proxy = result["control"], result["proxy"]
        if control and proxy and control.get("public_ip") and proxy["ip"]:
            result["same_address"] = control["public_ip"] == proxy["ip"]
        return result

    async def _look_at_solver(self, typed: str | None = None) -> dict[str, Any]:
        address = (
            typed if typed is not None else str(self.db.get_setting("captcha_solver_url"))
        ).rstrip("/")
        solver: dict[str, Any] = {
            "set": bool(address),
            "enabled": bool(self.db.get_setting("captcha_solver_enabled")),
            "ready": False,
            "version": "",
            "error": "",
        }
        if not address:
            return solver
        try:
            async with httpx.AsyncClient(timeout=8, transport=self.transport) as client:
                reply = await client.get(address + "/")
                reply.raise_for_status()
                data = reply.json()
            solver["ready"] = "ready" in str(data.get("msg", "")).lower()
            solver["version"] = str(data.get("version") or "")
            if not solver["ready"]:
                solver["error"] = "It answered, but not as FlareSolverr does."
        except httpx.HTTPStatusError as exc:
            solver["error"] = f"It answered HTTP {exc.response.status_code}."
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            solver["error"] = f"It cannot be reached ({exc or type(exc).__name__})."
        return solver

    async def status(self, fresh: bool = False) -> dict[str, Any]:
        """What the VPN and the solver say of themselves; asked at most every few seconds."""
        async with self._looking:
            stale = time.monotonic() - self._status_at > STATUS_TTL
            if fresh or stale or self._status is None:
                vpn, solver = await asyncio.gather(self._look_at_vpn(), self._look_at_solver())
                self._status = {"vpn": vpn, "solver": solver}
                self._status_at = time.monotonic()
            else:  # the switches and the change in progress are ours to know: always current
                self._status["vpn"].update(
                    enabled=bool(self.db.get_setting("build_proxy_enabled")),
                    change_ip=bool(self.db.get_setting("vpn_change_ip")),
                    changing=not self._steady.is_set(),
                )
                self._status["solver"]["enabled"] = bool(
                    self.db.get_setting("captcha_solver_enabled")
                )
            return self._status

    def forget(self) -> None:
        """Settings changed: what was seen before may no longer be true."""
        self._status_at = 0.0

    # -- another address ------------------------------------------------------------

    async def change_ip(self, since: int | None = None, forced: bool = False) -> bool:
        """Reconnect the VPN so that it comes back with another address.

        ``since`` is the :attr:`generation` the caller's refused request was sent under: if
        the address has changed since, there is nothing more to do. Returns whether the
        requests that follow leave from a different address than that one did.
        """
        if not self.control:
            raise NetworkError("Set the VPN's control server address in Settings first.")
        async with self._changing:
            if since is not None and self.generation > since:
                return True
            if not forced and time.monotonic() - self._changed_at < CHANGE_EVERY:
                return False  # the new address is being refused too: waiting is what is left
            self._steady.clear()
            self._publish()
            before = ""
            try:
                async with self._client(15) as client:
                    try:
                        before = (await self._public_ip(client))["public_ip"]
                    except (httpx.HTTPError, ValueError):
                        pass
                    await self._vpn_status(client)  # settles which route this Gluetun has
                    await self._set_vpn(client, "stopped")
                    await asyncio.sleep(1)
                    await self._set_vpn(client, "running")
                    after = await self._wait_for_address(client)
            except (httpx.HTTPError, ValueError, NetworkError) as exc:
                self.last_change_error = self._problem(exc)
                log.warning("changing the VPN address failed: %s", self.last_change_error)
                raise NetworkError(self.last_change_error) from exc
            finally:
                self._changed_at = time.monotonic()
                self._steady.set()
                self.forget()
                self._publish()
            self.generation += 1
            self.changes += 1
            self.last_change = time.time()
            changed = bool(after) and after != before
            self.last_change_error = (
                "" if changed else "The VPN reconnected with the same address as before."
            )
            log.info("VPN address changed: %s -> %s", before or "?", after or "?")
            self._publish()
            return changed

    async def _wait_for_address(self, client: httpx.AsyncClient) -> str:
        deadline = time.monotonic() + CHANGE_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            try:
                if await self._vpn_status(client) != "running":
                    continue
                address = (await self._public_ip(client))["public_ip"]
            except (httpx.HTTPError, ValueError):
                continue  # the control server is busy while the tunnel comes up
            if address:
                return address
        raise NetworkError(
            f"The VPN did not come back with an address within {CHANGE_TIMEOUT} seconds."
        )

    def _publish(self) -> None:
        self.events.publish("network", {"changing": not self._steady.is_set()})


class NetworkError(Exception):
    """A problem with the VPN's control server, worded for the person reading it."""
