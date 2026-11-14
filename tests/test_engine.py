import asyncio
import errno
import hashlib
import os

import httpx
import pytest

from zim_pro.downloads import metalink
from zim_pro.downloads.engine import DiskError, Download, DownloadError, RateLimiter
from zim_pro.downloads.metalink import Metalink

PIECE = 64 * 1024


def make_link(data: bytes, urls: list[str]) -> Metalink:
    pieces = [hashlib.sha1(data[i : i + PIECE]).hexdigest() for i in range(0, len(data), PIECE)]
    return Metalink("book.zim", len(data), urls, hashlib.sha256(data).hexdigest(), PIECE, pieces)


class _Stream(httpx.AsyncByteStream):
    def __init__(self, body: bytes):
        self.body = body

    async def __aiter__(self):
        yield self.body


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
        headers = {
            "Content-Range": f"bytes {start}-{end}/{len(self.data)}",
            "Content-Length": str(len(body)),
        }
        if self.fail_after is not None:
            body = body[: max(0, self.fail_after - self.served)]
            self.served += len(body)
            return httpx.Response(206, stream=_Stream(body), headers=headers)
        self.served += len(body)
        return httpx.Response(206, content=body, headers=headers)


def client_for(routes: dict[str, Mirror]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return routes[request.url.host](request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _seconds: real_sleep(0))
    monkeypatch.setattr("zim_pro.downloads.engine.SEGMENT_BYTES", 4 * PIECE)


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


async def test_requires_sha256(tmp_path):
    data = os.urandom(PIECE * 2)
    link = make_link(data, ["http://a/book.zim"])
    link.sha256 = None
    download = Download(link, tmp_path / "book.zim", client=client_for({"a": Mirror(data)}))
    with pytest.raises(DownloadError, match="SHA-256"):
        await download.run()


async def test_resumed_zeroed_part_rehashes_and_redownloads(tmp_path):
    data = os.urandom(PIECE * 3)
    link = make_link(data, ["http://a/book.zim"])
    part = tmp_path / "book.zim.part"
    part.write_bytes(bytes(len(data)))  # zero-filled part with stale bitmap
    mirror = Mirror(data)
    download = Download(
        link, tmp_path / "book.zim", pieces=b"\x07", client=client_for({"a": mirror})
    )
    await download.run()
    assert part.read_bytes() == data
    assert len(mirror.requests) >= 1


@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (416, {}),
        (206, {}),  # missing Content-Range
        (206, {"Content-Range": "bytes 0-10/999999"}),  # mismatched Content-Range
        (200, {"Content-Length": "100"}),  # 200 on partial or wrong length
    ],
)
async def test_invalid_range_responses_fail(tmp_path, status, headers):
    data = os.urandom(PIECE * 2)
    link = make_link(data, ["http://a/book.zim"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=data[:100], headers=headers)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    download = Download(link, tmp_path / "book.zim", connections=1, client=client)
    with pytest.raises(DownloadError):
        await download.run()


async def test_enospc_aborts_immediately_without_penalizing_mirrors(tmp_path, monkeypatch):
    data = os.urandom(PIECE * 2)
    link = make_link(data, ["http://a/book.zim", "http://b/book.zim"])

    def fake_pwrite(_fd, _data, _offset):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "pwrite", fake_pwrite)
    m_a, m_b = Mirror(data), Mirror(data)
    download = Download(
        link, tmp_path / "book.zim", connections=1, client=client_for({"a": m_a, "b": m_b})
    )
    with pytest.raises(DiskError, match="disk error"):
        await download.run()
    assert all(m.failures == 0 for m in download.mirrors)


async def test_trickling_mirror_aborts_on_timeout(tmp_path, monkeypatch):
    data = os.urandom(PIECE)
    link = make_link(data, ["http://a/book.zim"])
    monkeypatch.setattr("zim_pro.downloads.engine.SEGMENT_DEADLINE_BASE", 0.05)

    class TrickleStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield data[:10]
            await asyncio.sleep(5)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            206,
            stream=TrickleStream(),
            headers={
                "Content-Range": f"bytes 0-{len(data) - 1}/{len(data)}",
                "Content-Length": str(len(data)),
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    download = Download(link, tmp_path / "book.zim", connections=1, client=client)
    # Restore real sleep for asyncio.timeout to fire normally
    monkeypatch.setattr(asyncio, "sleep", asyncio.sleep)
    with pytest.raises(DownloadError):
        await asyncio.wait_for(download.run(), timeout=2.0)


def test_sync_safe_after_run_closes_fd(tmp_path):
    data = os.urandom(PIECE)
    link = make_link(data, ["http://a/book.zim"])
    download = Download(link, tmp_path / "book.zim", client=client_for({"a": Mirror(data)}))
    asyncio.run(download.run())
    download.sync()  # must not raise EBADF


async def test_rate_limiter_does_not_serialize_sleep(monkeypatch):
    # Use real sleep with a tiny delay to verify concurrent tasks don't block each other on lock
    limiter = RateLimiter(1000)
    await asyncio.gather(limiter.take(100), limiter.take(100))


@pytest.mark.parametrize(
    "bad_xml",
    [
        b"<html><body>not metalink</body></html>",
        (
            b'<metalink xmlns="urn:ietf:params:xml:ns:metalink">'
            b'<file name="a.zim"><size>abc</size></file></metalink>'
        ),
        (
            b'<metalink xmlns="urn:ietf:params:xml:ns:metalink"><file name="a.zim">'
            b'<size>10</size><hash type="sha-256">' + b"a" * 64 + b"</hash>"
            b'<pieces length="0" type="sha-1"><hash>' + b"b" * 40 + b"</hash></pieces>"
            b"<url>https://example.com/a.zim</url></file></metalink>"
        ),
        (
            b'<metalink xmlns="urn:ietf:params:xml:ns:metalink"><file name="a.zim">'
            b"<size>10</size><url>https://example.com/a.zim</url></file></metalink>"
        ),
        (
            b'<metalink xmlns="urn:ietf:params:xml:ns:metalink"><file name="a.zim">'
            b'<size>10</size><hash type="sha-256">' + b"a" * 64 + b"</hash>"
            b'<pieces length="4" type="sha-1"><hash>' + b"b" * 40 + b"</hash></pieces>"
            b"<url>https://example.com/a.zim</url></file></metalink>"
        ),
    ],
)
def test_metalink_rejects_malformed(bad_xml):
    with pytest.raises(ValueError):
        metalink.parse(bad_xml)


async def test_rate_limiter_sleeps_for_excess_tokens(monkeypatch):
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float):
        delays.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr("zim_pro.downloads.engine.asyncio.sleep", fake_sleep)
    limiter = RateLimiter(100)  # starts with 100 tokens
    await limiter.take(250)  # 150 tokens over -> ~1.5s delay
    assert len(delays) == 1
    assert 1.4 <= delays[0] <= 1.6


async def test_retry_backoff_all_mirrors_fail_and_checksum_mismatch(tmp_path, monkeypatch):
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float):
        delays.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr("zim_pro.downloads.engine.asyncio.sleep", fake_sleep)

    data = os.urandom(PIECE)
    # Mirror fails twice with 503, then succeeds -> backoff delays recorded
    good_mirror = Mirror(data)
    attempts = 0

    def flaky_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            return httpx.Response(503)
        return good_mirror(request)

    dl = Download(
        make_link(data, ["http://a/book.zim"]),
        tmp_path / "retry.zim",
        connections=1,
        client=httpx.AsyncClient(transport=httpx.MockTransport(flaky_handler)),
    )
    await dl.run()
    assert dl.part.read_bytes() == data
    assert len(delays) >= 2 and delays[1] >= delays[0]

    # All mirrors permanently fail -> DownloadError
    dl_fail = Download(
        make_link(data, ["http://a/book.zim"]),
        tmp_path / "fail.zim",
        connections=1,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _req: httpx.Response(503))),
    )
    with pytest.raises(DownloadError):
        await dl_fail.run()

    # Corrupt data on mirror -> piece verification fails -> DownloadError
    corrupt_mirror = Mirror(data, corrupt=True)
    dl_corrupt = Download(
        make_link(data, ["http://a/book.zim"]),
        tmp_path / "corrupt.zim",
        connections=1,
        client=client_for({"a": corrupt_mirror}),
    )
    with pytest.raises(DownloadError):
        await dl_corrupt.run()
