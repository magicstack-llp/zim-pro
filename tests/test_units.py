from pathlib import Path

import pytest

from kiwix_updater.catalog import opds
from kiwix_updater.catalog.updates import book_updates
from kiwix_updater.db import Database
from kiwix_updater.downloads import metalink
from kiwix_updater.downloads.engine import Bitmap
from kiwix_updater.library.article import resolve_link
from kiwix_updater.library.naming import split_filename

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("wikipedia_en_all_maxi_2024-01.zim", ("wikipedia_en_all_maxi", "2024-01")),
        ("100r.co_en_all_2025-03.zim.meta4", ("100r.co_en_all", "2025-03")),
        (
            "https://x.org/zim/devdocs/devdocs_en_bash_2026-10.zim.meta4",
            ("devdocs_en_bash", "2026-10"),
        ),
        ("custom.zim", ("custom", "")),
    ],
)
def test_split_filename(filename, expected):
    assert split_filename(filename) == expected


def test_metalink_parse():
    link = metalink.parse((FIXTURES / "bash.meta4").read_bytes())
    assert link.name == "devdocs_en_bash_2026-10.zim"
    assert link.size == 572117
    assert len(link.sha256) == 64
    assert link.piece_length == 4194304
    assert len(link.piece_hashes) == link.piece_count == 1
    assert link.piece_range(0) == (0, 572117)
    assert link.urls[0].startswith("https://") and len(link.urls) >= 2


def test_metalink_rejects_missing_mirrors():
    xml = b'<metalink xmlns="urn:ietf:params:xml:ns:metalink"><file name="a"><size>5</size></file></metalink>'
    with pytest.raises(ValueError):
        metalink.parse(xml)


def test_bitmap_roundtrip():
    bitmap = Bitmap(11)
    bitmap.set(0)
    bitmap.set(10)
    assert bitmap.missing() == list(range(1, 10))
    again = Bitmap(11, bitmap.to_bytes())
    assert again[0] and again[10] and not again[5]
    assert Bitmap(12, b"\xff").done_count() == 0  # wrong length is ignored


def test_resolve_link():
    assert resolve_link("A/Linux", "Kernel") == "A/Kernel"
    assert resolve_link("A/Linux", "../I/logo.png") == "I/logo.png"
    assert resolve_link("A/Linux", "https://example.org/") is None
    assert resolve_link("A/Linux", "#history") is None
    assert resolve_link("A/Linux", "../../etc") is None


def _book(db, **fields):
    row = {"id": "b", "path": "/x/b.zim", "filename": "b.zim", "size": 1, "mtime": 0,
           "name": "b", "flavour": "", "date": "2025-01-09", "uuid": "old"}  # fmt: skip
    row.update(fields)
    db.execute(
        f"INSERT INTO books ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
        list(row.values()),
    )


def test_opds_parse_and_update_matching():
    rows = opds.parse_feed(
        (FIXTURES / "feed.xml").read_bytes(), "https://library.kiwix.org/catalog/v2/entries"
    )
    assert len(rows) == 1
    entry = rows[0]
    assert entry["stem"] == "devdocs_en_bash" and entry["issued"].startswith("2026-10-05")
    assert entry["url"].endswith(".zim.meta4") and entry["size"] == 572416
    assert entry["illustration"].startswith("https://library.kiwix.org/catalog/v2/illustration/")

    db = Database(":memory:")
    opds.store(db, rows)
    _book(db, id="devdocs_en_bash", path="/x/a.zim")
    _book(db, id="gone_en_all", path="/x/g.zim", name="gone_en_all")
    _book(
        db,
        id="renamed",
        path="/x/r.zim",
        name="devdocs_en_bash",
        uuid=entry["uuid"],
        date="2026-10-05",
    )
    status = {item["book"]["id"]: item for item in book_updates(db)}
    assert status["devdocs_en_bash"]["status"] == "update"
    assert status["devdocs_en_bash"]["latest"]["uuid"] == entry["uuid"]
    assert status["gone_en_all"]["status"] == "unpublished"
    assert status["renamed"]["status"] == "current"  # matched by Name metadata, same uuid
