import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

NS = "{urn:ietf:params:xml:ns:metalink}"
DEFAULT_PIECE_LENGTH = 4 * 1024 * 1024


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
    root = ET.fromstring(xml)
    file = root.find(f"{NS}file")
    if file is None:
        raise ValueError("metalink has no <file> element")
    size = int(file.findtext(f"{NS}size") or 0)
    if size <= 0:
        raise ValueError("metalink has no file size")

    sha256 = None
    for node in file.findall(f"{NS}hash"):
        if node.get("type") == "sha-256" and node.text:
            sha256 = node.text.strip().lower()

    piece_length, piece_hashes = DEFAULT_PIECE_LENGTH, []
    pieces = file.find(f"{NS}pieces")
    if pieces is not None and pieces.get("type") == "sha-1":
        piece_length = int(pieces.get("length") or DEFAULT_PIECE_LENGTH)
        piece_hashes = [h.text.strip().lower() for h in pieces.findall(f"{NS}hash") if h.text]

    urls = sorted(
        (
            (int(node.get("priority") or 999), node.text.strip())
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
        link.piece_hashes = []  # inconsistent piece list: rely on the whole-file hash
    return link
