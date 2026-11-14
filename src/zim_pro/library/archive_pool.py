import threading
from collections import OrderedDict

from libzim.reader import Archive


class ArchivePool:
    """LRU cache of open ZIM archives, so each request does not reopen its file."""

    def __init__(self, max_open: int = 64):
        self._max_open = max_open
        self._archives: OrderedDict[str, Archive] = OrderedDict()
        self._generations: dict[str, int] = {}
        self._global_gen = 0
        self._lock = threading.Lock()

    def get(self, path: str) -> Archive:
        for _ in range(4):
            with self._lock:
                archive = self._archives.get(path)
                if archive is not None:
                    self._archives.move_to_end(path)
                    return archive
                gen = (self._global_gen, self._generations.get(path, 0))
            archive = Archive(path)
            with self._lock:
                if (self._global_gen, self._generations.get(path, 0)) != gen:
                    # Invalidated while opening; discard and re-open the current file.
                    continue
                existing = self._archives.get(path)
                if existing is not None:
                    self._archives.move_to_end(path)
                    return existing
                self._archives[path] = archive
                self._archives.move_to_end(path)
                while len(self._archives) > self._max_open:
                    self._archives.popitem(last=False)
                return archive
        return archive

    def invalidate(self, path: str) -> None:
        with self._lock:
            self._archives.pop(path, None)
            self._generations[path] = self._generations.get(path, 0) + 1

    def clear(self) -> None:
        with self._lock:
            self._archives.clear()
            self._global_gen += 1
