import asyncio
import json
from typing import Any


class EventBus:
    """Fan-out of server events to every connected SSE client."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: str, data: Any) -> None:
        """Send an event to every subscriber; safe to call from any thread."""
        loop = self._loop
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if loop is not None and not on_loop:
            loop.call_soon_threadsafe(self._publish, event, data)
        else:
            self._publish(event, data)

    def _publish(self, event: str, data: Any) -> None:
        message = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass  # a slow client misses a progress tick; the next one catches it up
