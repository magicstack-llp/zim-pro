import string
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

NS = "{urn:ietf:params:xml:ns:metalink}"
DEFAULT_PIECE_LENGTH = 4 * 1024 * 1024
_HEX = frozenset(string.hexdigits)


def _is_hex(value: str | None, length: int) -> bool:
    return bool(value) and len(value) == length and all(c in _HEX for c in value)


def _parse_int(value: str | None, field_name: str, default: int | None = None) -> int:
    if value is None or not value.strip():
        if default is not None:
            return default
        raise ValueError(f"invalid metalink: missing {field_name}")
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ValueError(f"invalid metalink {field_name}: {value!r}") from exc


@dataclass
class Metalink:
    name: str
    size: int
    urls: list[str]
    sha256: str | None = None
    piece_length: int = DEFAULT_PIECE_LENGTH
    piece_hashes: list[str] = field(default_factory=list)

    @property
    def piece_count(self) -> int:
        return max(1, -(-self.size // self.piece_length))

    def piece_range(self, index: int) -> tuple[int, int]:
        """Byte offset and length of one piece."""
        start = index * self.piece_length
        return start, min(self.piece_length, self.size - start)


def parse(xml: bytes) -> Metalink:
    """Parse a Metalink 4 (RFC 5854) document describing a single file."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f"invalid metalink XML: {exc}") from exc
    files = root.findall(f"{NS}file")
    if not files:
        raise ValueError("metalink has no <file> element")
    file = next((f for f in files if (f.get("name") or "").endswith(".zim")), files[0])

    size = _parse_int(file.findtext(f"{NS}size"), "size", default=0)
    if size <= 0:
        raise ValueError("metalink has no valid file size")

    sha256 = None
    for node in file.findall(f"{NS}hash"):
        if node.get("type") == "sha-256" and node.text:
            candidate = node.text.strip().lower()
            if _is_hex(candidate, 64):
                sha256 = candidate
                break
    if not sha256:
        raise ValueError("metalink has no valid sha-256 hash")

    piece_length, piece_hashes = DEFAULT_PIECE_LENGTH, []
    pieces = file.find(f"{NS}pieces")
    if pieces is not None and pieces.get("type") == "sha-1":
        piece_length = _parse_int(
            pieces.get("length"), "piece length", default=DEFAULT_PIECE_LENGTH
        )
        if piece_length <= 0:
            raise ValueError(f"invalid metalink piece length: {piece_length}")
        piece_hashes = [h.text.strip().lower() for h in pieces.findall(f"{NS}hash") if h.text]
        if any(not _is_hex(h, 40) for h in piece_hashes):
            raise ValueError("invalid metalink piece hash")

    def _priority(raw: str | None) -> int:
        try:
            return int(raw) if raw and raw.strip() else 999
        except ValueError:
            return 999

    urls = sorted(
        (
            (_priority(node.get("priority")), node.text.strip())
            for node in file.findall(f"{NS}url")
            if node.text and node.text.strip().startswith(("http://", "https://"))
        ),
        key=lambda pair: pair[0],
    )
    if not urls:
        raise ValueError("metalink lists no HTTP mirrors")

    link = Metalink(
        name=file.get("name") or "",
        size=size,
        urls=[url for _, url in urls],
        sha256=sha256,
        piece_length=piece_length,
        piece_hashes=piece_hashes,
    )
    if piece_hashes and len(piece_hashes) != link.piece_count:
        raise ValueError(
            f"invalid metalink piece count: expected {link.piece_count}, got {len(piece_hashes)}"
        )
    return link
