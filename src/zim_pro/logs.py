"""One place for every log line: kept in memory for Settings, in a file, and in Redis.

Each line says which part of Zim Pro wrote it (its area), and the job it was written for
when there is one: everything a feed sync logs, down to the page fetches, carries
``feed 12``. Secrets that end up in a message (keys, tokens, passwords) are blanked out
before the line is kept anywhere.
"""

import contextlib
import contextvars
import json
import logging
import logging.handlers
import re
import threading
import time
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any

KEEP = 5000  # lines kept in memory
FILE_BYTES = 5 * 1024 * 1024
FILE_COUNT = 5
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# The area a line belongs to, from the name of the logger that wrote it; the first match wins.
AREAS = (
    ("zim_pro.downloads", "downloads"),
    ("zim_pro.api.system", "queues"),
    ("zim_pro.api.library", "settings"),
    ("zim_pro.feeds", "feeds"),
    ("zim_pro.builder", "builds"),
    ("zim_pro.network", "network"),
    ("zim_pro.queues", "queues"),
    ("zim_pro.monitor", "monitoring"),
    ("zim_pro.catalog", "catalog"),
    ("zim_pro.library", "library"),
    ("zim_pro.scheduler", "scheduler"),
    ("zim_pro.mcp", "mcp"),
    ("zim_pro.http", "requests"),
    ("zim_pro.api.ai", "ai"),
    ("zim_pro.ai", "ai"),
    ("zim_pro.providers", "ai"),
    ("zim_pro.retrieval", "ai"),
    ("zim_pro.websearch", "ai"),
    ("httpx", "web"),
    ("httpcore", "web"),
    ("uvicorn", "server"),
    ("mcp", "mcp"),
)

JOB_AREAS = {"feed": "feeds", "build": "builds", "download": "downloads"}

_SECRETS = (
    re.compile(r"(?i)\b(bearer\s+)[\w.~+/=-]{8,}"),
    re.compile(
        r"(?i)\b((?:api[_-]?key|token|password|secret|key)[\"']?\s*[=:]\s*[\"']?)[^\s\"'&,}]+"
    ),
    re.compile(r"\b(sk-|gsk_|AIza|xai-|pplx-)[\w-]{10,}"),
)

_job: contextvars.ContextVar[str] = contextvars.ContextVar("log_job", default="")


@contextlib.contextmanager
def job(name: str) -> Iterator[None]:
    """Tag every line logged inside, by this task and the tasks and threads it starts."""
    token = _job.set(name)
    try:
        yield
    finally:
        _job.reset(token)


def redact(text: str) -> str:
    for pattern in _SECRETS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "•••", text)
    return text


def area_of(logger: str) -> str:
    for prefix, area in AREAS:
        if logger == prefix or logger.startswith(prefix + "."):
            return area
    return "system"


class _Quiet(logging.Filter):
    """The web client's line for every request is detail: thousands come with one crawl."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "httpx" and record.levelno == logging.INFO:
            record.levelno, record.levelname = logging.DEBUG, "DEBUG"
        return True


class LogHub(logging.Handler):
    """Keeps the last :data:`KEEP` lines and hands them to the Logs section of Settings."""

    def __init__(self) -> None:
        super().__init__()
        self._lines: deque[dict[str, Any]] = deque(maxlen=KEEP)
        self._next = 1
        self._lock_lines = threading.Lock()
        self._file: logging.handlers.RotatingFileHandler | None = None
        self.path: Path | None = None
        self._outbox: deque[dict[str, Any]] = deque(maxlen=KEEP)  # for Redis, when it is used
        self.counts: dict[str, int] = dict.fromkeys(LEVELS, 0)
        self.started = time.time()

    # -- taking lines in ---------------------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info and record.exc_info[0] is not None:
                message += "\n" + logging.Formatter().formatException(record.exc_info)
            line = {
                "time": record.created,
                "level": record.levelname,
                "area": "",
                "logger": record.name,
                "job": getattr(record, "job", "") or _job.get(),
                "message": redact(message)[:20000],
            }
            # A job's lines belong with it, whichever module wrote them.
            line["area"] = JOB_AREAS.get(line["job"].split(" ")[0], "") or area_of(record.name)
            with self._lock_lines:
                line["id"] = self._next
                self._next += 1
                self._lines.append(line)
                self._outbox.append(line)
                self.counts[record.levelname] = self.counts.get(record.levelname, 0) + 1
            if self._file is not None:
                self._file.emit(_Line(line))
        except Exception:  # a log line must never take down what wrote it
            self.handleError(record)

    def attach_file(self, folder: Path) -> None:
        """Write every line to ``folder/zim-pro.log`` as JSON, and read back the last ones."""
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / "zim-pro.log"
        self._read_back(self.path)
        handler = logging.handlers.RotatingFileHandler(
            self.path, maxBytes=FILE_BYTES, backupCount=FILE_COUNT, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._file = handler

    def _read_back(self, path: Path) -> None:
        """The lines from before a restart: the Logs section shows them too."""
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                handle.seek(max(0, handle.tell() - 4 * 1024 * 1024))
                tail = handle.read().decode("utf-8", "replace").splitlines()[-KEEP:]
        except OSError:
            return
        with self._lock_lines:
            for text in tail:
                try:
                    line = json.loads(text)
                except ValueError:
                    continue
                if isinstance(line, dict) and "message" in line:
                    line["id"] = self._next
                    self._next += 1
                    self._lines.append(line)

    def take_outbox(self) -> list[dict[str, Any]]:
        with self._lock_lines:
            lines = list(self._outbox)
            self._outbox.clear()
        return lines

    # -- giving them out ---------------------------------------------------------------

    def query(
        self,
        level: str = "",
        area: str = "",
        text: str = "",
        job_name: str = "",
        after: int = 0,
        before: int = 0,
        limit: int = 300,
    ) -> list[dict[str, Any]]:
        """Lines newest first, at ``level`` or above, matching every filter given."""
        lowest = LEVELS.index(level) if level in LEVELS else 0
        needle = text.lower()
        with self._lock_lines:
            lines = list(self._lines)
        found: list[dict[str, Any]] = []
        for line in reversed(lines):
            if after and line["id"] <= after:
                break
            if before and line["id"] >= before:
                continue
            if line["level"] in LEVELS and LEVELS.index(line["level"]) < lowest:
                continue
            if area and line["area"] != area:
                continue
            if job_name and line["job"] != job_name:
                continue
            if needle and needle not in line["message"].lower() and needle not in line["job"]:
                continue
            found.append(line)
            if len(found) >= limit:
                break
        return found

    def summary(self, since: float) -> dict[str, Any]:
        """How many lines of each level, and of each area, since ``since``."""
        with self._lock_lines:
            lines = [line for line in self._lines if line["time"] >= since]
        levels: dict[str, int] = {}
        areas: dict[str, int] = {}
        for line in lines:
            levels[line["level"]] = levels.get(line["level"], 0) + 1
            areas[line["area"]] = areas.get(line["area"], 0) + 1
        return {"levels": levels, "areas": areas}

    def areas(self) -> list[str]:
        with self._lock_lines:
            seen = {line["area"] for line in self._lines}
        return sorted(seen | {area for _, area in AREAS} | {"system"})

    def jobs(self) -> list[str]:
        with self._lock_lines:
            names = [line["job"] for line in self._lines if line["job"]]
        return list(dict.fromkeys(reversed(names)))[:50]

    @property
    def newest(self) -> int:
        return self._next - 1

    def clear(self) -> None:
        with self._lock_lines:
            self._lines.clear()

    def files(self) -> list[Path]:
        if self.path is None:
            return []
        rotated = [self.path.with_name(f"{self.path.name}.{n}") for n in range(FILE_COUNT, 0, -1)]
        return [file for file in [*rotated, self.path] if file.exists()]


class _Line(logging.LogRecord):
    """A kept line, as the record the file handler writes: one JSON object."""

    def __init__(self, line: dict[str, Any]):
        super().__init__("zim_pro", logging.INFO, "", 0, "%s", (json.dumps(line),), None)


class _Redacting(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


hub = LogHub()
_console = logging.StreamHandler()
_console.setFormatter(_Redacting("%(asctime)s %(levelname)s %(name)s: %(message)s"))


def set_level(level: str) -> None:
    """The least a line must be to be kept. Detail (DEBUG) includes every web request."""
    level = level.upper() if level.upper() in LEVELS else "INFO"
    logging.getLogger().setLevel(level)
    hub.setLevel(level)
    _console.setLevel(level)


def setup(level: str = "INFO", folder: Path | None = None, console: bool = True) -> LogHub:
    """Send every logger to the hub (and the console); call once, at start."""
    root = logging.getLogger()
    if hub not in root.handlers:
        root.addHandler(hub)
        if console and not root.handlers[:-1]:  # none of its own yet (mcp mode has one)
            root.addHandler(_console)
        logging.getLogger("httpx").addFilter(_Quiet())
        logging.getLogger("httpcore").setLevel(logging.WARNING)  # a line per socket call
        # Each request is logged by the server itself (see main.py), with how long it took.
        logging.getLogger("uvicorn.access").disabled = True
    if folder is not None and hub.path is None:
        try:
            hub.attach_file(folder)
        except OSError as exc:
            logging.getLogger(__name__).warning("logs are not written to a file: %s", exc)
    set_level(level)
    return hub
