"""What the book converters share: the look of a page and the book's front page."""

import html
from pathlib import Path

from ..writer import Stage

BOOK_CSS = """
body{font:18px/1.65 Georgia,"Times New Roman",serif;color:#1c1c1e;background:#fff;
max-width:44rem;margin:0 auto;padding:1.5rem 1rem 4rem}
img{max-width:100%;height:auto}h1,h2,h3{line-height:1.25;font-family:system-ui,sans-serif}
a{color:#0f766e}.zp-nav{display:flex;justify-content:space-between;gap:1rem;margin:2.5rem 0 0;
padding-top:1rem;border-top:1px solid #e4e4e7;font:14px system-ui,sans-serif}
.zp-cover{max-height:22rem;display:block;margin:0 auto 1.5rem}.zp-meta{color:#71717a;
font:15px system-ui,sans-serif}.zp-toc{font-family:system-ui,sans-serif;line-height:1.9}
.zp-page-image{border:1px solid #e4e4e7;margin-bottom:1rem}
@media(prefers-color-scheme:dark){body{background:#18181b;color:#e4e4e7}a{color:#5eead4}
.zp-nav{border-color:#3f3f46}}
"""

escape = html.escape


def document(title: str, body: str, language: str = "en", head: str = "") -> str:
    return (
        f'<!doctype html><html lang="{escape(language)}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>{head}</head><body>{body}</body></html>"
    )


def nav_bar(previous: str, contents: str, following: str) -> str:
    """Previous / contents / next links for the foot of a chapter; any may be empty."""
    left = f'<a href="{previous}">← Previous</a>' if previous else "<span></span>"
    right = f'<a href="{following}">Next →</a>' if following else "<span></span>"
    return f'<nav class="zp-nav">{left}<a href="{contents}">Contents</a>{right}</nav>'


def keep_original(stage: Stage, source: Path, filename: str, mimetype: str) -> str:
    """Store the uploaded file itself in the ZIM; returns its path there."""
    path = f"original/{filename}"
    stage.add_resource(path, mimetype, source)
    return path


def front_page(
    stage: Stage,
    title: str,
    author: str,
    description: str,
    contents: list[tuple[str, str]],
    original: str,
    language: str = "en",
    cover: str = "",
    notice: str = "",
) -> None:
    """The book's main page: cover, who wrote it, the contents, and the original file."""
    parts = []
    if cover:
        parts.append(f'<img class="zp-cover" src="{escape(cover)}" alt="Cover">')
    parts.append(f"<h1>{escape(title)}</h1>")
    if author:
        parts.append(f'<p class="zp-meta">{escape(author)}</p>')
    if description:
        parts.append(f"<p>{escape(description)}</p>")
    if notice:
        parts.append(f'<p class="zp-meta"><strong>{escape(notice)}</strong></p>')
    rows = "".join(f'<li><a href="{escape(p)}">{escape(t)}</a></li>' for p, t in contents)
    parts.append(f'<h2>Contents</h2><ol class="zp-toc">{rows}</ol>')
    name = original.rsplit("/", 1)[-1]
    parts.append(
        f'<p class="zp-meta">Original file: <a href="{escape(original)}">{escape(name)}</a></p>'
    )
    text = " ".join([title, author, description, *(t for _, t in contents)])
    head = f"<style>{BOOK_CSS}</style>"
    stage.add_page("index", title, document(title, "".join(parts), language, head), text)
    stage.main = "index"
