import threading
from collections import OrderedDict

from libzim.reader import Archive


class ArchivePool:
    """LRU cache of open ZIM archives, so each request does not reopen its file."""

    def __init__(self, max_open: int = 64):
        self._max_open = max_open
        self._archives: OrderedDict[str, Archive] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, path: str) -> Archive:
        with self._lock:
            archive = self._archives.get(path)
            if archive is not None:
                self._archives.move_to_end(path)
                return archive
        archive = Archive(path)
        with self._lock:
            self._archives[path] = archive
            self._archives.move_to_end(path)
            while len(self._archives) > self._max_open:
                self._archives.popitem(last=False)
        return archive

    def invalidate(self, path: str) -> None:
        with self._lock:
            self._archives.pop(path, None)

    def clear(self) -> None:
        with self._lock:
            self._archives.clear()
