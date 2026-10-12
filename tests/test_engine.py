import asyncio
import hashlib
import os

import httpx
import pytest

from kiwix_updater.downloads.engine import Download, DownloadError
from kiwix_updater.downloads.metalink import Metalink

PIECE = 64 * 1024


def make_link(data: bytes, urls: list[str]) -> Metalink:
    pieces = [hashlib.sha1(data[i : i + PIECE]).hexdigest() for i in range(0, len(data), PIECE)]
    return Metalink("book.zim", len(data), urls, hashlib.sha256(data).hexdigest(), PIECE, pieces)


class Mirror:
    """In-process HTTP mirror that honours Range and can misbehave on demand."""

    def __init__(self, data: bytes, corrupt: bool = False, fail_after: int | None = None):
        self.data, self.corrupt, self.fail_after = data, corrupt, fail_after
        self.requests: list[tuple[int, int]] = []
        self.served = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        start, end = (int(x) for x in request.headers["range"].removeprefix("bytes=").split("-"))
        self.requests.append((start, end))
        body = self.data[start : end + 1]
        if self.corrupt:
            body = bytes(len(body))
        if self.fail_after is not None:
            body = body[: max(0, self.fail_after - self.served)]
        self.served += len(body)
        return httpx.Response(206, content=body)


def client_for(routes: dict[str, Mirror]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return routes[request.url.host](request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _seconds: real_sleep(0))
    monkeypatch.setattr("kiwix_updater.downloads.engine.SEGMENT_BYTES", 4 * PIECE)


async def test_downloads_and_verifies(tmp_path):
    data = os.urandom(PIECE * 10 + 1234)
    mirror = Mirror(data)
    download = Download(
        make_link(data, ["http://a/book.zim"]),
        tmp_path / "book.zim",
        connections=1,
        client=client_for({"a": mirror}),
    )
    await download.run()
    assert download.part.read_bytes() == data
    assert download.bytes_done == len(data) and not download.bitmap.missing()
    assert len(mirror.requests) == 3  # 11 pieces in runs of 4


async def test_resumes_without_refetching(tmp_path):
    data = os.urandom(PIECE * 8)
    link = make_link(data, ["http://a/book.zim"])
    flaky = Mirror(data, fail_after=PIECE * 3 + 100)
    first = Download(link, tmp_path / "book.zim", connections=1, client=client_for({"a": flaky}))
    with pytest.raises(DownloadError):
        await first.run()
    assert first.bitmap.done_count() == 3

    good = Mirror(data)
    second = Download(
        link, tmp_path / "book.zim", pieces=first.bitmap.to_bytes(), client=client_for({"a": good})
    )
    await second.run()
    assert second.part.read_bytes() == data
    assert min(start for start, _ in good.requests) == PIECE * 3  # the first 3 pieces were kept


async def test_corrupt_mirror_is_abandoned(tmp_path):
    data = os.urandom(PIECE * 6)
    bad, good = Mirror(data, corrupt=True), Mirror(data)
    link = make_link(data, ["http://bad/book.zim", "http://good/book.zim"])
    download = Download(link, tmp_path / "book.zim", client=client_for({"bad": bad, "good": good}))
    await download.run()
    assert download.part.read_bytes() == data


async def test_stale_bitmap_is_caught_by_final_checksum(tmp_path):
    data = os.urandom(PIECE * 4)
    link = make_link(data, ["http://a/book.zim"])
    part = tmp_path / "book.zim.part"
    part.write_bytes(bytes(len(data)))  # claims to be complete, holds zeros
    download = Download(
        link, tmp_path / "book.zim", pieces=b"\x0f", client=client_for({"a": Mirror(data)})
    )
    await download.run()
    assert part.read_bytes() == data


async def test_missing_part_file_resets_progress(tmp_path):
    data = os.urandom(PIECE * 2)
    link = make_link(data, ["http://a/book.zim"])
    download = Download(
        link, tmp_path / "book.zim", pieces=b"\x03", client=client_for({"a": Mirror(data)})
    )
    await download.run()
    assert download.part.read_bytes() == data
