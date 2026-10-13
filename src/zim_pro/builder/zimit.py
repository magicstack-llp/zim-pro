"""Browser capture with Zimit, for sites that only render with JavaScript.

Zimit is Kiwix's own tool: it drives a real browser and records pages as shown. It ships
only as a container image, so it is run through the Docker Engine API on the local socket.
Talking to that socket is control of the host's Docker, which is why this engine is off
unless the socket has been made available on purpose.
"""

import asyncio
import os
import re
import socket
from pathlib import Path
from typing import Any

import httpx

from .report import Reporter
from .writer import BuildError, language_code, slugify

IMAGE = "ghcr.io/openzim/zimit"
SOCKET = Path(os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock"))
_SCOPES = {"page": "page", "path": "prefix", "site": "host"}
_STATS = re.compile(r'"crawled"\s*:\s*(\d+).*?"total"\s*:\s*(\d+)')


def unavailable() -> str:
    """Why browser capture cannot be used here; empty when it can."""
    if not SOCKET.exists():
        return "Docker is not reachable from Zim Pro (no Docker socket)."
    if not os.access(SOCKET, os.R_OK | os.W_OK):
        return "Zim Pro is not allowed to use the Docker socket."
    return ""


def _client() -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(uds=str(SOCKET))
    return httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=None)


async def _self_container(client: httpx.AsyncClient) -> str:
    """This process's own container id, if it runs in one (so volumes can be shared)."""
    reply = await client.get(f"/containers/{socket.gethostname()}/json")
    return reply.json().get("Id", "") if reply.status_code == 200 else ""


async def capture(link: dict[str, Any], folder: Path, report: Reporter, name: str) -> Path:
    """Capture ``link`` into a ZIM inside ``folder`` and return the file."""
    reason = unavailable()
    if reason:
        raise BuildError(f"Browser capture is not available: {reason}")
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o777)  # Zimit writes as its own user; the folder stays ours to tidy up
    title = link["title"] or re.sub(r"^https?://(www\.)?", "", link["url"]).strip("/")
    scope = _SCOPES.get(link["scope"], "page")
    if scope == "host" and link["subdomains"]:
        scope = "domain"
    command = [
        "zimit",
        "--seeds", link["url"],
        "--name", name,
        "--title", title[:30],
        "--description", f"Saved from {link['url']}"[:80],
        "--zim-lang", language_code(link["language"]),
        "--scopeType", scope,
        "--pageLimit", str(1 if link["scope"] == "page" else link["max_pages"]),
        "--depth", str(0 if link["scope"] == "page" else link["max_depth"]),
        "--workers", "2",
        "--output", "/output",
    ]  # fmt: skip
    async with _client() as client:
        report.phase("preparing the browser")
        try:
            async with client.stream(
                "POST", "/images/create", params={"fromImage": IMAGE, "tag": "latest"}
            ) as pull:
                if pull.status_code != 200:
                    raise BuildError(
                        f"The Zimit image could not be downloaded (HTTP {pull.status_code})."
                    )
                async for _ in pull.aiter_lines():
                    report.check()
        except httpx.HTTPError as exc:
            raise BuildError(f"Docker could not be reached: {exc}") from exc
        host: dict[str, Any] = {"ShmSize": 1024**3}
        own = await _self_container(client)
        output = "/output"
        if own:
            # Inside a container the folder is not a host path, so share our volumes.
            host["VolumesFrom"] = [own]
            output = str(folder)
            command[-1] = output
        else:
            host["Binds"] = [f"{folder.resolve()}:/output"]
        created = await client.post(
            "/containers/create",
            params={"name": f"zim-pro-zimit-{slugify(name, 40)}-{os.getpid()}"},
            json={"Image": f"{IMAGE}:latest", "Cmd": command, "HostConfig": host},
        )
        if created.status_code != 201:
            raise BuildError(f"The capture could not be started: {created.text[:200]}")
        container = created.json()["Id"]
        try:
            await client.post(f"/containers/{container}/start")
            report.phase("capturing", 1 if link["scope"] == "page" else link["max_pages"])
            tail = ""
            async with client.stream(
                "GET",
                f"/containers/{container}/logs",
                params={"follow": "1", "stdout": "1", "stderr": "1"},
            ) as logs:
                async for chunk in logs.aiter_bytes():
                    report.check()
                    tail = (tail + chunk.decode("utf-8", "replace"))[-4000:]
                    for match in _STATS.finditer(tail):
                        report.progress(int(match.group(1)), max(int(match.group(2)), 1))
            status = (await client.post(f"/containers/{container}/wait")).json()
            if status.get("StatusCode") != 0:
                lines = [line for line in tail.splitlines() if line.strip()]
                last = re.sub(r"[^\x20-\x7e]", "", lines[-1])[:300] if lines else "no output"
                raise BuildError(f"The capture failed: {last}")
        finally:
            await asyncio.shield(client.delete(f"/containers/{container}", params={"force": "1"}))
    files = sorted(folder.glob("*.zim"), key=lambda f: f.stat().st_mtime)
    if not files:
        raise BuildError("The capture finished but produced no ZIM file.")
    return files[-1]
