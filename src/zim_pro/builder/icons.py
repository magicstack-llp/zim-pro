"""Pictures the user picks for the ZIMs made here, kept beside the rest of the state."""

import io
from pathlib import Path

from PIL import Image

from .writer import BuildError

KINDS = {"link": "links", "upload": "uploads", "feed": "feeds"}  # kind -> its table
SIZE = 256  # kept larger than the 48 pixels a ZIM holds, so the pages here can show it well
MAX_BYTES = 10 * 1024 * 1024


def path(state_dir: Path, kind: str, source_id: int) -> Path:
    return state_dir / "icons" / f"{kind}-{int(source_id)}.png"


def readable(data: bytes) -> bool:
    """Whether ``data`` is a picture that can be drawn (an SVG, for one, is not)."""
    try:
        with Image.open(io.BytesIO(data)) as picture:
            picture.verify()
        return True
    except Exception:
        return False


def normalise(data: bytes) -> bytes:
    """Any picture as a square PNG; raises :class:`BuildError` if it is not a picture."""
    try:
        picture = Image.open(io.BytesIO(data))
        picture.seek(0)
        picture = picture.convert("RGBA")
    except Exception as exc:
        raise BuildError("Choose a PNG, JPEG, WebP, GIF or ICO picture.") from exc
    picture.thumbnail((SIZE, SIZE), Image.LANCZOS)
    side = max(picture.size)
    tile = Image.new("RGBA", (side, side), (255, 255, 255, 0))
    tile.paste(picture, ((side - picture.width) // 2, (side - picture.height) // 2), picture)
    out = io.BytesIO()
    tile.save(out, "PNG")
    return out.getvalue()


def load(state_dir: Path, kind: str, source_id: int) -> bytes | None:
    try:
        return path(state_dir, kind, source_id).read_bytes()
    except OSError:
        return None
