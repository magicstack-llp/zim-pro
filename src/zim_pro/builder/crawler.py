"""The built-in crawler: fetch a page, a section or a site, and stage it for a ZIM.

Fetching is concurrent (several requests in flight, one host never hammered); cleaning each
page runs on worker threads. Every address ends up saved, skipped with a reason, or failed
with an error, and the reporter keeps that account. Fetched files are cached per link with
their validators, so rebuilding to update a ZIM only downloads what changed.

A site is asked what it wants before its pages are fetched: ``robots.txt`` says what may be
fetched and how fast, and its sitemaps list pages that links alone might not reach. What
sitemaps list and what pages link to go through one queue that holds each address once. A
site that answers "too many requests" is given the pause it asks for, and a slower pace after.
"""

import asyncio
import email.utils
import hashlib
import json
import logging
import mimetypes
import re
import time
import urllib.robotparser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..downloads.engine import USER_AGENT
from ..feeds import parse as feed_parse
from . import extract, icons, sitemap
from .report import Cancelled, Reporter
from .writer import BuildError, Stage, ZimMeta, language_code, slugify

log = logging.getLogger(__name__)

MB = 1024 * 1024
PAGE_LIMIT_BYTES = 15 * MB
HOST_DELAY = 0.12  # seconds between two requests to the same host
RETRIES = 2
# "Too many requests" (HTTP 429, or 503 with Retry-After): wait as long as the site says, or
# RATE_WAIT doubling each time it happens again, and go slower on that host afterwards.
RATE_RETRIES = 6
RATE_WAIT = 5.0
RATE_WAIT_MAX = 300.0
SLOW_DELAY_MIN = 1.0  # the pace a host is first slowed to
SLOW_DELAY_MAX = 8.0  # the slowest pace a host is slowed to, in seconds between requests
CALM_AFTER = 30  # replies without complaint before the pace is picked up again
CRAWL_DELAY_MAX = 10.0  # the longest Crawl-delay from robots.txt that is taken literally
_DOCUMENTS = re.compile(r"\.(pdf|epub|txt|csv|json|xml|docx?|xlsx?|pptx?|odt|ods|md|zip)$", re.I)
_PICTURES = re.compile(r"\.(png|jpe?g|gif|webp|svg|avif|bmp|ico)$", re.I)
_MEDIA = re.compile(r"\.(mp4|webm|ogv|m4v|mov|mp3|ogg|oga|opus|m4a|wav|flac)$", re.I)


@dataclass
class CrawlOptions:
    url: str
    scope: str = "page"  # page | path | site
    max_pages: int = 500
    max_depth: int = 5
    clean: bool = True
    images: bool = True
    video: bool = False
    max_file_mb: int = 50
    max_total_mb: int = 2048
    respect_robots: bool = True
    subdomains: bool = False  # with scope "site": blog.example.org along with example.org
    sitemaps: bool = True
    # Sitemaps to read as given, in place of looking for them; and where the rules are, if
    # not at /robots.txt.
    sitemap_urls: tuple[str, ...] = ()
    robots_url: str = ""
    # RSS or Atom feeds of the site: the articles they list are saved along with the rest.
    feed_urls: tuple[str, ...] = ()
    # Update from the feeds alone: fetch only the articles that are new, and take every
    # page saved before from the cache. Needs a build made earlier to start from.
    feeds_only: bool = False
    rate: float = 0  # requests per second to one host; 0 leaves it to the site and to us
    proxy: str = ""  # every request goes through it, such as a VPN container's HTTP proxy
    solver: str = ""  # a FlareSolverr address, asked when a site puts up a browser check
    # The VPN behind the proxy (a ``network.Network``): waited for while it reconnects,
    # and asked for another address when a site keeps answering "too many requests".
    network: Any = None
    title: str = ""
    language: str = ""


@dataclass
class Fetched:
    url: str  # where the request ended up, after redirects
    content_type: str
    file: Path
    cached: bool = False

    @property
    def is_html(self) -> bool:
        return self.content_type in ("text/html", "application/xhtml+xml")


class FetchError(Exception):
    pass


class NothingNew(Exception):
    """An update from the feeds found no article that is not already saved."""


FEED_TRIES = 3  # times an article from a feed is asked for before it is given up on


class _Challenge(Exception):
    """The site answered with a browser check (a "captcha" page) in place of the page."""


_CHALLENGE_MARKS = (
    b"just a moment",
    b"challenge-platform",
    b"cf-chl",
    b"checking your browser",
    b"attention required",
    b"ddos-guard",
    b"enable javascript and cookies to continue",
)
SOLVER_TIMEOUT = 90  # seconds a solver may take over one page


class _SlowDown(Exception):
    """The site answered "too many requests"; ``wait`` is what it asked for, if it said."""

    def __init__(self, status: int, wait: float | None):
        super().__init__(f"HTTP {status}")
        self.status, self.wait = status, wait


def _retry_after(value: str) -> float | None:
    """A Retry-After header as seconds from now: it is a number or a date."""
    value = value.strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


class Fetcher:
    """HTTP with a per-link cache, a size limit, retries and a pause between hits on a host.

    The pause is per host and adapts: it starts at ``HOST_DELAY`` (or the site's Crawl-delay),
    grows when the host says it is being asked too much, and shrinks back once it is calm.
    """

    def __init__(
        self,
        cache: Path,
        workers: int,
        transport: httpx.AsyncBaseTransport | None = None,
        gate: Callable[[], Awaitable[None]] | None = None,
        on_slow: Callable[[str, float, int], None] | None = None,
        pace: float = 0,
        proxy: str = "",
        solver: str = "",
        network: Any = None,
        on_new_address: Callable[[str], None] | None = None,
    ):
        self._network = network if proxy else None
        self._on_new_address = on_new_address
        self.address_changes = 0
        self.pace = pace  # seconds between requests to a host, when the user set a rate
        self.solver = solver.rstrip("/")
        # Use what is in the cache without asking again: for content that never changes.
        self.prefer_cache = False
        self.solved = 0
        self._agents: dict[str, str] = {}  # the browser a host's clearance was given to
        self._solving = asyncio.Semaphore(1)
        self._solver_client = httpx.AsyncClient(timeout=SOLVER_TIMEOUT + 15, transport=transport)
        self._gate = gate
        self._on_slow = on_slow
        self._base: dict[str, float] = {}  # the pace a host asked for in robots.txt
        self._delay: dict[str, float] = {}  # the pace it is held to now
        self._blocked: dict[str, float] = {}  # no request to the host before this moment
        self._strikes: dict[str, int] = {}
        self._calm: dict[str, int] = {}
        self.slowed = 0
        self.cache = cache
        self.cache.mkdir(parents=True, exist_ok=True)
        self._index_file = cache / "index.json"
        try:
            self._index: dict[str, dict[str, str]] = json.loads(self._index_file.read_text())
        except (OSError, ValueError):
            self._index = {}
        self._slots = asyncio.Semaphore(max(1, workers))
        self._hosts: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        self.client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en,*;q=0.5"},
            follow_redirects=True,
            timeout=httpx.Timeout(30, connect=15),
            limits=httpx.Limits(max_connections=max(4, workers * 2)),
            transport=transport,
            proxy=proxy or None,
        )
        self.downloaded = 0
        self.reused = 0

    async def close(self) -> None:
        await self.client.aclose()
        await self._solver_client.aclose()
        self._index_file.write_text(json.dumps(self._index))

    def set_pace(self, host: str, seconds: float) -> None:
        """Never ask ``host`` more often than this (its Crawl-delay).

        A rate the user set for the link is their decision and is not overridden.
        """
        if self.pace:
            return
        self._base[host] = max(HOST_DELAY, min(seconds, CRAWL_DELAY_MAX))
        self._delay[host] = max(self._delay.get(host, 0), self._base[host])

    async def _wait_turn(self, host: str) -> None:
        lock = self._hosts.setdefault(host, asyncio.Lock())
        async with lock:
            while True:
                pace = self._delay.get(host, self.pace or HOST_DELAY)
                ready = max(self._last.get(host, 0) + pace, self._blocked.get(host, 0))
                wait = ready - time.monotonic()
                if wait <= 0:
                    break
                await asyncio.sleep(min(wait, 1.0))  # short naps: a pause must be noticed
                if self._gate:
                    await self._gate()
            self._last[host] = time.monotonic()

    def _slow_down(self, host: str, status: int, asked: float | None) -> None:
        now = time.monotonic()
        if self._blocked.get(host, 0) > now:
            return  # several requests were refused together: one pause answers them all
        strikes = self._strikes.get(host, 0)
        wait = asked if asked is not None else RATE_WAIT * 2**strikes
        wait = min(max(wait, RATE_WAIT if asked is None else 0.0), RATE_WAIT_MAX)
        self._strikes[host] = strikes + 1
        self._calm[host] = 0
        self._blocked[host] = now + wait
        base = self._base.get(host, self.pace or HOST_DELAY)
        self._delay[host] = min(
            max(self._delay.get(host, base) * 2, SLOW_DELAY_MIN), SLOW_DELAY_MAX
        )
        self.slowed += 1
        if self._on_slow:
            self._on_slow(host, wait, status)

    async def _new_address(self, host: str, since: int) -> bool:
        """Have the VPN change its address after ``host`` refused; True if it now differs."""
        network = self._network
        if network is None or not network.changes_ip_when_refused:
            return False
        try:
            if not await network.change_ip(since):
                return False
        except Exception as exc:  # the VPN's trouble must not end the build
            log.warning("could not change the VPN address: %s", exc)
            return False
        # A new address starts with a clean record: no pause owed, the ordinary pace, and
        # none of the clearances the old address had earned.
        self._blocked.pop(host, None)
        self._strikes[host] = 0
        self._calm.pop(host, None)
        self._delay.pop(host, None)
        if host in self._base:
            self._delay[host] = self._base[host]
        self._agents.clear()
        self.client.cookies.clear()
        self.address_changes += 1
        if self._on_new_address:
            self._on_new_address(host)
        return True

    def _went_well(self, host: str) -> None:
        if host not in self._delay or host not in self._calm:
            return
        self._calm[host] += 1
        if self._calm[host] >= CALM_AFTER:
            self._calm[host] = 0
            self._strikes[host] = max(0, self._strikes.get(host, 0) - 1)
            base = self._base.get(host, self.pace or HOST_DELAY)
            self._delay[host] = max(base, self._delay[host] / 2)

    def cache_file(self, url: str) -> Path:
        return self.cache / hashlib.sha1(url.encode()).hexdigest()

    @staticmethod
    async def _challenged(reply: httpx.Response) -> bool:
        if reply.status_code not in (403, 503):
            return False
        if reply.headers.get("cf-mitigated", "").lower() == "challenge":
            return True
        if "html" not in reply.headers.get("content-type", ""):
            return False
        body = (await reply.aread())[:40000].lower()
        return any(mark in body for mark in _CHALLENGE_MARKS)

    async def _solve(self, url: str) -> tuple[str, str]:
        """Have the solver open ``url`` in its browser; returns (final address, page).

        The clearance it earns (cookies, and the browser they were given to) is kept, so
        later requests to the site can go straight through.
        """
        async with self._solving:
            try:
                reply = await self._solver_client.post(
                    f"{self.solver}/v1",
                    json={"cmd": "request.get", "url": url, "maxTimeout": SOLVER_TIMEOUT * 1000},
                )
                data = reply.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise FetchError(
                    f"the captcha solver could not be reached ({exc or type(exc).__name__})"
                ) from exc
        solution = data.get("solution") or {}
        if data.get("status") != "ok" or not solution.get("response"):
            why = str(data.get("message") or "no page came back")[:200]
            raise FetchError(f"the captcha solver could not get past the site's check: {why}")
        if int(solution.get("status") or 200) >= 400:
            raise FetchError(f"HTTP {solution['status']}")
        for cookie in solution.get("cookies") or []:
            if cookie.get("name"):
                self.client.cookies.set(
                    cookie["name"], cookie.get("value", ""), domain=cookie.get("domain", "")
                )
        if solution.get("userAgent"):
            self._agents[urlsplit(url).netloc] = solution["userAgent"]
        self.solved += 1
        return extract.normalise(solution.get("url") or url) or url, solution["response"]

    async def get(self, url: str, limit: int, fresh: bool = False) -> Fetched:
        """Fetch ``url`` into the cache; raises :class:`FetchError` with a reason to show.

        ``fresh`` asks the site even when :attr:`prefer_cache` would use the copy at hand.
        """
        file = self.cache_file(url)
        known = self._index.get(url) if file.exists() else None
        if known and self.prefer_cache and not fresh:
            self.reused += 1
            return Fetched(known["url"], known["type"], file, cached=True)
        headers = {}
        solved: tuple[str, str] | None = None
        if known:
            if known.get("etag"):
                headers["If-None-Match"] = known["etag"]
            if known.get("modified"):
                headers["If-Modified-Since"] = known["modified"]
        last_error = "no reply"
        host = urlsplit(url).netloc
        attempt = refused = 0
        while attempt <= RETRIES:
            async with self._slots:
                if self._gate:
                    await self._gate()
                await self._wait_turn(host)
                if self._network is not None:
                    await self._network.steady()
                sent_under = self._network.generation if self._network is not None else 0
                if host in self._agents:
                    headers["User-Agent"] = self._agents[host]
                try:
                    async with self.client.stream("GET", url, headers=headers) as reply:
                        if await self._challenged(reply):
                            raise _Challenge
                        if reply.status_code == 304 and known:
                            self.reused += 1
                            self._went_well(host)
                            return Fetched(known["url"], known["type"], file, cached=True)
                        asked = _retry_after(reply.headers.get("retry-after", ""))
                        if reply.status_code == 429 or (
                            reply.status_code == 503 and asked is not None
                        ):
                            raise _SlowDown(reply.status_code, asked)
                        if reply.status_code in (500, 502, 503, 504):
                            last_error = f"HTTP {reply.status_code}"
                            raise httpx.TransportError(last_error)
                        if reply.status_code >= 400:
                            raise FetchError(f"HTTP {reply.status_code}")
                        declared = int(reply.headers.get("content-length") or 0)
                        if declared > limit:
                            raise FetchError(f"larger than {limit // MB} MB")
                        size = 0
                        partial = file.with_suffix(".part")
                        with partial.open("wb") as out:
                            async for chunk in reply.aiter_bytes(256 * 1024):
                                size += len(chunk)
                                if size > limit:
                                    raise FetchError(f"larger than {limit // MB} MB")
                                out.write(chunk)
                        partial.replace(file)
                        kind = reply.headers.get("content-type", "").split(";")[0].strip().lower()
                        if not kind or kind == "application/octet-stream":
                            kind = mimetypes.guess_type(urlsplit(url).path)[0] or kind
                        final = extract.normalise(str(reply.url)) or url
                        self._index[url] = {
                            "url": final,
                            "type": kind or "application/octet-stream",
                            "etag": reply.headers.get("etag", ""),
                            "modified": reply.headers.get("last-modified", ""),
                        }
                        self.downloaded += size
                        self._went_well(host)
                        return Fetched(final, self._index[url]["type"], file)
                except _Challenge:
                    if not self.solver:
                        raise FetchError(
                            "the site answered with a browser check (a captcha page); "
                            "set a captcha solver in Settings to get past it"
                        ) from None
                    if solved is None:
                        # Earn a clearance, then ask again the ordinary way: that keeps the
                        # real bytes for pictures and files, which a browser would wrap.
                        solved = await self._solve(url)
                        continue
                    # Still checked on every request: keep the page the solver's browser saw.
                    final, page = solved
                    file.write_text(page, "utf-8")
                    self._index[url] = {
                        "url": final,
                        "type": "text/html",
                        "etag": "",
                        "modified": "",
                    }
                    self.downloaded += len(page)
                    return Fetched(final, "text/html", file)
                except _SlowDown as slow:
                    # Not one of the ordinary retries: the site is there, it wants a pause.
                    refused += 1
                    if refused > RATE_RETRIES:
                        raise FetchError(
                            f"HTTP {slow.status}: the site kept refusing, even after waiting"
                        ) from None
                    if await self._new_address(host, sent_under):
                        solved = None
                        continue
                    self._slow_down(host, slow.status, slow.wait)
                    continue
                except FetchError:
                    file.with_suffix(".part").unlink(missing_ok=True)
                    raise
                except (httpx.HTTPError, OSError) as exc:
                    file.with_suffix(".part").unlink(missing_ok=True)
                    last_error = (
                        last_error if "HTTP" in last_error else (str(exc) or type(exc).__name__)
                    )
            attempt += 1
            if attempt <= RETRIES:
                await asyncio.sleep(1.5 * attempt)
        raise FetchError(last_error)


def _decode(data: bytes, content_type_header: str = "") -> str:
    match = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", data[:4096], re.I)
    for encoding in (match.group(1).decode("ascii", "ignore") if match else "", "utf-8"):
        try:
            if encoding:
                return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace")


class Crawler:
    def __init__(
        self,
        options: CrawlOptions,
        stage: Stage,
        cache: Path,
        report: Reporter,
        workers: int = 4,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.options = options
        self.stage = stage
        self.report = report
        self.workers = max(1, workers)
        self.fetcher = Fetcher(
            cache,
            self.workers,
            transport,
            report.gate,
            self._slowed,
            pace=1 / options.rate if options.rate > 0 else 0,
            proxy=options.proxy,
            solver=options.solver,
            network=options.network,
            on_new_address=self._new_address,
        )
        self.seed = extract.normalise(options.url)
        if not self.seed:
            raise BuildError(f"{options.url!r} is not a web address")
        seed = urlsplit(self.seed)
        self.host = seed.netloc.removeprefix("www.")
        last = seed.path.rsplit("/", 1)[-1]
        # "Under this path": a file's folder, or the path itself when it names a section.
        self.prefix = (seed.path.rsplit("/", 1)[0] if "." in last else seed.path).rstrip("/") + "/"
        self.saved: dict[str, str] = {}  # address -> ZIM path, for pages and files alike
        self.page_urls: dict[str, str] = {}  # ZIM path -> the address it was saved from
        self.resources: dict[str, str] = {}  # address -> kind, still to fetch
        self.first: extract.Prepared | None = None
        self.icon_urls: list[str] = []  # where to look for the ZIM's picture, in order
        # The one queue of pages to fetch, and every address that has ever been offered to it.
        self.queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
        self.seen: set[str] = set()
        self._open = 0  # pages queued and not yet dealt with
        self.from_sitemaps = 0
        self.from_feeds = 0  # articles saved because a feed listed them
        self.fetched_pages: list[str] = []  # every address a saved page was asked for by
        self._feed_new: set[str] = set()
        self._manifest_file = cache / "manifest.json"
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._met: dict[str, asyncio.Task[list[str]]] = {}
        self._sitemap_tasks: list[asyncio.Task[None]] = []
        self._slow_hosts: set[str] = set()
        self._missing: set[str] = set()  # files that could not be had before: not asked again

    # -- scope ----------------------------------------------------------------------

    def _same_site(self, netloc: str) -> bool:
        host = netloc.removeprefix("www.")
        if host == self.host:
            return True
        wide = self.options.subdomains and self.options.scope == "site"
        return wide and host.endswith("." + self.host)

    def in_scope(self, url: str) -> bool:
        parts = urlsplit(url)
        if not self._same_site(parts.netloc):
            return False
        if self.options.scope == "site":
            return True
        if self.options.scope == "path":
            return (parts.path.rstrip("/") + "/").startswith(self.prefix) or url == self.seed
        return url == self.seed

    def allowed(self, url: str) -> bool:
        robots = self._robots.get(urlsplit(url).netloc.removeprefix("www."))
        return robots is None or robots.can_fetch(USER_AGENT, url)

    @staticmethod
    def _key(url: str) -> str:
        """What makes two addresses the same page: http or https, with or without www."""
        parts = urlsplit(url)
        return f"{parts.netloc.removeprefix('www.')}{parts.path}?{parts.query}"

    def _new_address(self, host: str) -> None:
        self.report.note(
            f"{host} answered too many requests: the VPN was given another address and "
            "the page was asked for again."
        )

    def _slowed(self, host: str, wait: float, status: int) -> None:
        if host not in self._slow_hosts:
            self._slow_hosts.add(host)
            self.report.note(
                f"{host} answered HTTP {status} (too many requests). Waiting as long as it "
                "asks before trying again, and fetching more slowly from here on."
            )
        if wait >= 1:
            self.report.waiting(f"{host} asked for a pause of {round(wait)} s", wait)

    # -- robots.txt and sitemaps ----------------------------------------------------

    async def _meet(self, scheme: str, netloc: str) -> None:
        """Learn a host's rules before anything else is asked of it. Once per host.

        With and without ``www.`` count as one host: its rules are read once, from the
        spelling that was met first.
        """
        name = netloc.removeprefix("www.")
        task = self._met.get(name)
        if task is None:
            task = self._met[name] = asyncio.create_task(self._load_robots(scheme, netloc))
            listed = await task
            options = self.options
            crawl = options.scope != "page" and options.max_depth and not options.feeds_only
            if options.sitemaps and crawl:
                reading = asyncio.create_task(self._read_sitemaps(scheme, netloc, listed))
                self._sitemap_tasks.append(reading)
        else:
            await task

    async def _load_robots(self, scheme: str, netloc: str) -> list[str]:
        """Read ``robots.txt``; returns the sitemaps it names."""
        name = netloc.removeprefix("www.")
        self._robots[name] = None
        address = f"{scheme}://{netloc}/robots.txt"
        if self.options.robots_url and netloc == urlsplit(self.seed).netloc:
            address = extract.normalise(self.options.robots_url) or address
        try:
            fetched = await self.fetcher.get(address, MB, fresh=True)
        except FetchError:
            return []
        if fetched.is_html:
            return []  # a "not found" page, not rules
        text = _decode(fetched.file.read_bytes())
        if self.options.respect_robots:
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(text.splitlines())
            self._robots[name] = parser
            pace = parser.crawl_delay(USER_AGENT)
            rate = parser.request_rate(USER_AGENT)
            if not pace and rate and rate.requests:
                pace = rate.seconds / rate.requests
            if pace:
                for spelling in (name, "www." + name):
                    self.fetcher.set_pace(spelling, float(pace))
                if float(pace) >= 1:
                    self.report.note(
                        f"{netloc} asks for {float(pace):g} s between requests; "
                        f"fetching at one every {min(float(pace), CRAWL_DELAY_MAX):g} s."
                    )
        return sitemap.listed_in_robots(text)

    async def _read_sitemaps(self, scheme: str, netloc: str, listed: list[str]) -> None:
        """Queue the pages a host's sitemaps list. Never ends the build if it goes wrong."""
        try:
            await self._sitemaps(scheme, netloc, listed)
        except (Cancelled, asyncio.CancelledError):
            raise
        except Exception as exc:
            self.report.note(f"The sitemap of {netloc} could not be read: {exc}")

    async def _sitemaps(self, scheme: str, netloc: str, listed: list[str]) -> None:
        root = f"{scheme}://{netloc}"
        own = netloc == urlsplit(self.seed).netloc
        given = list(self.options.sitemap_urls) if own else []
        named = [u for u in (extract.normalise(a, root + "/") for a in given + listed) if u]
        # Sitemaps the user named are the ones to read: nothing is guessed on top of them.
        guesses = [] if given else [root + path for path in sitemap.USUAL_PATHS]
        if own and not given:
            # A section published on its own (a manual under /docs/) keeps its sitemap there.
            folders = self.prefix.strip("/").split("/") if self.prefix.strip("/") else []
            near = ["/" + "/".join(folders[:n]) for n in range(len(folders), 0, -1)][:4]
            guesses[:0] = [f"{root}{f}/{n}" for f in near for n in ("sitemap.xml", "sitemap.txt")]
        todo = named + [g for g in guesses if g not in named]
        read: set[str] = set()
        pages: dict[str, None] = {}  # in order, each once
        # Enough to fill the page limit even if some fail; the rest could never be saved.
        room = self.options.max_pages * 2
        files = total = 0
        while todo and files < sitemap.MAX_FILES and len(pages) < room:
            address = todo.pop(0)
            if address in read:
                continue
            read.add(address)
            guessed = address in guesses and address not in named
            if guessed and files:
                break  # a sitemap was found; the other usual places need not be tried
            try:
                fetched = await self.fetcher.get(address, 20 * MB)
            except FetchError:
                continue
            more, found = await asyncio.to_thread(
                lambda f=fetched: sitemap.parse(f.file.read_bytes())
            )
            if not more and not found:
                continue
            files += 1
            total += len(found)
            todo[:0] = [u for u in (extract.normalise(m, address) for m in more) if u]
            for page in found:
                url = extract.normalise(page, address)
                if url and self.in_scope(url):
                    pages.setdefault(url)
        if not files:
            return
        added = await self._offer_all(list(pages)[:room], 1)
        self.from_sitemaps += added
        self.report.note(
            f"{netloc}: {total} addresses in {files} sitemap {'file' if files == 1 else 'files'}; "
            f"{len(pages)} are within what this link saves and {added} were new to the queue."
        )

    # -- feeds ------------------------------------------------------------------------

    def _manifest(self) -> dict:
        try:
            return json.loads(self._manifest_file.read_text())
        except (OSError, ValueError):
            return {}

    def can_update_from_feeds(self) -> bool:
        return bool(self._manifest().get("pages"))

    async def _feed_articles(self) -> list[str]:
        """The articles the site's feeds list that belong to this site, newest feed first."""
        found: dict[str, None] = {}
        for address in self.options.feed_urls:
            url = extract.normalise(address)
            if not url:
                continue
            try:
                fetched = await self.fetcher.get(url, 20 * MB, fresh=True)
                data = fetched.file.read_bytes()
                feed = await asyncio.to_thread(feed_parse.parse, data, fetched.url)
            except (FetchError, feed_parse.NotAFeed) as exc:
                self.report.failed(url, f"the feed could not be read: {exc}")
                continue
            for entry in feed.entries:
                if entry.url and self._same_site(urlsplit(entry.url).netloc):
                    found.setdefault(entry.url)
        return list(found)

    async def _queue_from_feeds(self) -> None:
        """Fill the queue for an update from the feeds: what was saved, and what is new."""
        manifest = self._manifest()
        tries: dict[str, int] = manifest.get("feed_tries", {})
        known = {self._key(page) for page in manifest["pages"]}
        articles = await self._feed_articles()
        fresh = [
            url
            for url in articles
            if self._key(url) not in known and tries.get(url, 0) < FEED_TRIES and self.allowed(url)
        ]
        if not fresh:
            raise NothingNew
        self._feed_new = set(fresh)
        # Everything saved before comes back from the cache without a request; only the
        # new articles, and the first page (it may list them), are asked for.
        self.fetcher.prefer_cache = True
        depth = self.options.max_depth  # as deep as it goes: links are not followed
        for url in [*manifest["pages"], *fresh]:
            key = self._key(url)
            if key not in self.seen:
                self.seen.add(key)
                self._open += 1
                self.queue.put_nowait((url, depth))
        self._missing = set(manifest.get("missing", []))

    def _write_manifest(self, wanted: set[str]) -> None:
        """Note what this build saved, for the next update from the feeds to start from."""
        old = self._manifest()
        tries: dict[str, int] = old.get("feed_tries", {})
        saved = {self._key(page) for page in self.fetched_pages}
        for url in self._feed_new:
            if self._key(url) in saved:
                tries.pop(url, None)
            else:
                tries[url] = tries.get(url, 0) + 1
        missing = (self._missing | wanted) - set(self.saved)
        through_feeds = set(old.get("feed_pages", [])) | self._feed_new
        manifest = {
            "pages": self.fetched_pages,
            "feed_pages": [page for page in self.fetched_pages if page in through_feeds],
            "missing": sorted(missing),
            "feed_tries": tries,
        }
        self._manifest_file.write_text(json.dumps(manifest))

    # -- the queue ------------------------------------------------------------------

    async def _offer_all(self, links: list[str], depth: int) -> int:
        """Offer addresses to the queue, meeting any host not seen before first."""
        fresh = [u for u in links if self._key(u) not in self.seen and self.in_scope(u)]
        for parts in {urlsplit(u)[:2] for u in fresh}:
            await self._meet(*parts)
        return sum(self._offer(link, depth) for link in fresh)

    def _offer(self, link: str, depth: int) -> bool:
        """Queue a page unless it was offered before; returns whether it was queued."""
        key = self._key(link)
        if key in self.seen:
            return False
        self.seen.add(key)
        path = urlsplit(link).path
        if _PICTURES.search(path) or _MEDIA.search(path):
            return False  # shown through the page that embeds it, if any does
        if _DOCUMENTS.search(path):
            self.resources.setdefault(link, "file")
            return False
        if not self.allowed(link):
            self.report.skipped(link, "not allowed by robots.txt")
            return False
        self._open += 1
        self.queue.put_nowait((link, depth))
        return True

    # -- pages ----------------------------------------------------------------------

    async def run(self) -> ZimMeta:
        try:
            return await self._run()
        finally:
            for task in [*self._met.values(), *self._sitemap_tasks]:
                task.cancel()
            await asyncio.gather(*self._met.values(), *self._sitemap_tasks, return_exceptions=True)
            await self.fetcher.close()

    async def _run(self) -> ZimMeta:
        options, report = self.options, self.report
        queue = self.queue
        report.phase("reading the site's rules")
        seed = urlsplit(self.seed)
        await self._meet(seed.scheme, seed.netloc)
        if not self.allowed(self.seed):
            raise BuildError("The site's robots.txt does not allow this address to be fetched.")
        # The page that was asked for goes first; then what the sitemaps list.
        self.seen.add(self._key(self.seed))
        self._open += 1
        queue.put_nowait((self.seed, 0 if not options.feeds_only else options.max_depth))
        page_limit = options.max_pages
        if options.feeds_only:
            await self._queue_from_feeds()
            page_limit = max(page_limit, self._open)  # news is added, never traded for old
        else:
            await asyncio.gather(*self._sitemap_tasks)
            if options.feed_urls:
                # What the feeds list now, and what they brought in before: a full build
                # must not lose articles that have since dropped off the end of a feed.
                articles = await self._feed_articles()
                kept = [u for u in self._manifest().get("feed_pages", []) if u not in articles]
                self._feed_new = set(articles) | set(kept)
                for url in [*articles, *kept]:
                    key = self._key(url)
                    if key not in self.seen and self.allowed(url):
                        self.seen.add(key)
                        self._open += 1
                        queue.put_nowait((url, options.max_depth))
        report.phase("fetching", min(page_limit, queue.qsize()))
        pages_done = 0
        limit_noted = False

        async def work() -> None:
            nonlocal pages_done, limit_noted
            while True:
                url, depth = await queue.get()
                try:
                    await report.gate()
                    if pages_done >= page_limit:
                        if not limit_noted:
                            limit_noted = True
                            report.note(f"Stopped at the limit of {page_limit} pages.")
                        report.skipped(url, "over the page limit")
                        continue
                    links = await self._page(url)
                    if links is None:
                        continue
                    pages_done += 1
                    waiting = pages_done + self._open - 1  # this one is still counted open
                    report.progress(pages_done, max(1, min(page_limit, waiting)))
                    if depth < options.max_depth:
                        await self._offer_all(links, depth + 1)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # one bad page must not end the build
                    report.failed(url, str(exc) or type(exc).__name__)
                finally:
                    self._open -= 1
                    queue.task_done()

        workers = [asyncio.create_task(work()) for _ in range(self.workers)]
        try:
            while True:
                await queue.join()
                # A host met along the way may still be reading its sitemaps.
                reading = [task for task in self._sitemap_tasks if not task.done()]
                if reading:
                    await asyncio.wait(reading)
                elif not self._open:
                    break
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        report.check()
        if not self.stage.pages:
            last = next((e["why"] for e in report.log if e["kind"] == "failed"), "")
            raise BuildError(f"The page could not be fetched{': ' + last if last else '.'}")

        for address in self._missing:
            self.resources.pop(address, None)
        wanted = set(self.resources)
        await self._resources()
        self._write_manifest(wanted)
        report.phase("converting", len(self.stage.pages))
        await self._localise()
        first = self.first
        assert first is not None
        title = options.title or first.title or self.host
        return ZimMeta(
            name=f"custom_{slugify(title)}",
            title=title,
            description=first.description or f"Saved from {self.seed}",
            language=language_code(options.language or first.language),
            creator=self.host,
            source=self.seed,
        )

    async def _page(self, url: str) -> list[str] | None:
        """Fetch and stage one page; returns the pages it links to, or None if not saved."""
        try:
            # The first page is always asked for: it is where a site shows what is new.
            fetched = await self.fetcher.get(url, PAGE_LIMIT_BYTES, fresh=url == self.seed)
        except FetchError as exc:
            self.report.failed(url, str(exc))
            return None
        if not fetched.is_html:
            # Not a page after all (a download behind a plain link): keep it as a file.
            self.resources.setdefault(url, "file")
            return None
        final = fetched.url
        if final != url and (not self.in_scope(final) and url != self.seed):
            self.report.skipped(url, f"redirects out of scope to {final}")
            return None
        path = extract.zim_path(final)
        if path in self.page_urls:
            self.saved[url] = path  # another address for a page already saved
            return None
        options = self.options
        prepared = await asyncio.to_thread(
            lambda: extract.prepare(
                _decode(fetched.file.read_bytes()),
                final,
                options.clean,
                options.images,
                options.video,
            )
        )
        if not prepared.text.strip() and not prepared.resources:
            self.report.skipped(url, "the page has no content (it may need JavaScript)")
            return None
        if prepared.fell_back:
            self.report.note("Kept the whole page: the article could not be told apart.", final)
        self.stage.add_page(path, prepared.title, prepared.html, prepared.text)
        self.page_urls[path] = final
        self.fetched_pages.append(url)
        self.from_feeds += url in self._feed_new
        for address in {url, final, prepared.canonical} - {""}:
            self.saved.setdefault(address, path)
        if self.first is None or url == self.seed:
            self.first = prepared
            self.stage.main = path
            self.icon_urls = [
                *prepared.icons,
                extract.normalise("/favicon.ico", final),
                prepared.share_picture,
            ]
        for address, kind in prepared.resources:
            self.resources.setdefault(address, kind)
        return prepared.links

    # -- pictures, media, stylesheets and linked documents ---------------------------

    async def _resources(self) -> None:
        options, report = self.options, self.report
        budget = options.max_total_mb * MB
        file_limit = options.max_file_mb * MB
        pending = dict(self.resources)
        report.phase("fetching files", len(pending))
        done = 0
        over_budget = False

        async def one(address: str, kind: str) -> None:
            nonlocal done, over_budget
            try:
                report.check()
                if address in self.saved:
                    return
                if self.stage.size_estimate > budget:
                    if not over_budget:
                        over_budget = True
                        report.note(f"Stopped saving files at the {options.max_total_mb} MB limit.")
                    report.skipped(address, "over the size limit for this ZIM")
                    return
                limit = file_limit * (10 if kind == "media" else 1)
                fetched = await self.fetcher.get(address, limit)
                path = extract.zim_path(fetched.url)
                if fetched.is_html and kind != "file":
                    report.skipped(address, "a web page was returned in place of the file")
                    return
                if kind == "style":
                    css = _decode(fetched.file.read_bytes())
                    for extra in extract.css_resources(css, fetched.url):
                        if extra not in pending and extra not in more:
                            more[extra] = "font" if not _PICTURES.search(extra) else "image"
                    styles[address] = (fetched.url, path, css)
                    self.saved[address] = path
                    return
                if not self.stage.has(path):
                    self.stage.add_resource(path, fetched.content_type, fetched.file)
                    self.stage.size_estimate += fetched.file.stat().st_size
                self.saved[address] = path
            except FetchError as exc:
                report.failed(address, str(exc))
            finally:
                done += 1
                report.progress(done)

        more: dict[str, str] = {}
        styles: dict[str, tuple[str, str, str]] = {}
        await asyncio.gather(*(one(a, k) for a, k in pending.items()))
        if more:  # what the stylesheets asked for
            report.progress(done, done + len(more))
            await asyncio.gather(*(one(a, k) for a, k in more.items()))
        for final, path, css in styles.values():
            self.stage.add_resource(
                path, "text/css", extract.localise_css(css, final, path, self.saved).encode()
            )
        await self._icon()

    async def _icon(self) -> None:
        """Take the first of the site's pictures that can be had and drawn."""
        for address in [a for a in self.icon_urls if a][:6]:
            try:
                icon = await self.fetcher.get(address, 2 * MB)
            except FetchError:
                continue
            data = icon.file.read_bytes()
            if icons.readable(data):
                self.stage.icon = data
                return
        # None: the ZIM gets a tile with the first letter of its title.

    async def _localise(self) -> None:
        """Point every page at what was saved, on worker threads."""
        slots = asyncio.Semaphore(self.workers)
        done = 0

        async def one(path: str) -> None:
            nonlocal done
            async with slots:
                await self.report.gate()
                await asyncio.to_thread(self._localise_one, path)
                done += 1
                if done % 20 == 0:
                    self.report.progress(done)

        await asyncio.gather(*(one(path) for path in list(self.stage.pages)))
        self.report.progress(done)

    def _localise_one(self, path: str) -> None:
        entry = self.stage.entries[path]
        html = extract.localise(entry.file.read_text("utf-8"), path, self.saved)
        entry.file.write_text(html, "utf-8")
