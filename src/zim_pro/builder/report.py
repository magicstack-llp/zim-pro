"""What a build tells the job that runs it: where it is, and what did not make it."""

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any

LOG_LIMIT = 500  # entries kept per build; the counts stay exact beyond it


class Cancelled(Exception):
    """The build was cancelled by the user."""


class Reporter:
    """Progress and the account of every page: saved, skipped or failed."""

    def __init__(self, on_change: Callable[[], None] | None = None):
        self.phase_name = "starting"
        self.done = 0
        self.total = 0
        self.failed_count = 0
        self.skipped_count = 0
        self.log: list[dict[str, str]] = []
        self.cancelled = threading.Event()
        self.paused = threading.Event()
        self._detail = ""
        self._detail_until = 0.0
        self._on_change = on_change
        self._lock = threading.Lock()

    def _changed(self) -> None:
        if self._on_change:
            self._on_change()

    def check(self) -> None:
        """Stop here if the build was cancelled; on a worker thread, wait here while paused."""
        if self.cancelled.is_set():
            raise Cancelled
        if self.paused.is_set() and not _on_event_loop():
            while self.paused.is_set():
                if self.cancelled.wait(0.2):
                    raise Cancelled

    async def gate(self) -> None:
        """The same for code on the event loop: wait while paused, without blocking it."""
        self.check()
        while self.paused.is_set():
            await asyncio.sleep(0.2)
            self.check()

    def waiting(self, text: str, seconds: float) -> None:
        """Say why nothing is moving for the next ``seconds`` (a site asked for a pause)."""
        self._detail, self._detail_until = text, time.monotonic() + seconds
        self._changed()

    def phase(self, name: str, total: int = 0) -> None:
        self.check()
        self.phase_name, self.done, self.total = name, 0, total
        self._changed()

    def progress(self, done: int, total: int | None = None) -> None:
        self.done = done
        if total is not None:
            self.total = total
        self._changed()

    def _note(self, kind: str, target: str, why: str) -> None:
        with self._lock:
            if len(self.log) < LOG_LIMIT:
                self.log.append({"kind": kind, "target": target[:500], "why": why[:300]})

    def failed(self, target: str, why: str) -> None:
        self.failed_count += 1
        self._note("failed", target, why)

    def skipped(self, target: str, why: str) -> None:
        self.skipped_count += 1
        self._note("skipped", target, why)

    def note(self, text: str, target: str = "") -> None:
        self._note("note", target, text)

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self.phase_name,
            "done": self.done,
            "total": self.total,
            "failed": self.failed_count,
            "skipped": self.skipped_count,
            "detail": self._detail if time.monotonic() < self._detail_until else "",
        }


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True
