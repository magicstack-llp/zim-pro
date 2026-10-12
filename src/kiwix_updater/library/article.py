import posixpath
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from markdownify import markdownify

from .reader import Article, clean_tree


def resolve_link(article_path: str, href: str) -> str | None:
    """Turn a link found in an article into the entry path it points to."""
    parts = urlsplit(href)
    if parts.scheme or parts.netloc or not parts.path:
        return None
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(article_path), parts.path))
    return None if joined.startswith("..") else unquote(joined)


def to_markdown(article: Article) -> str:
    """Convert an article to Markdown, keeping only its readable content."""
    if not article.mimetype.startswith("text/html"):
        if article.mimetype.startswith("text/"):
            return article.content.decode("utf-8", "replace")
        return f"[{article.mimetype} content, {len(article.content)} bytes]"
    tree = clean_tree(article.content)
    for image in tree.css("img"):
        image.decompose()
    root = tree.body or tree.root
    if root is None:
        return ""
    text = markdownify(root.html or "", heading_style="ATX", strip=["a"])
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def outline(article: Article, max_links: int = 200) -> dict[str, Any]:
    """Headings and internal links of an article, for navigating without reading it all."""
    if not article.mimetype.startswith("text/html"):
        return {"headings": [], "links": []}
    tree = clean_tree(article.content)
    headings = []
    for node in tree.css("h1, h2, h3, h4, h5, h6"):
        text = node.text(strip=True)
        if text:
            headings.append({"level": int(node.tag[1]), "text": text})
    links: dict[str, str] = {}
    for node in tree.css("a[href]"):
        target = resolve_link(article.path, node.attributes.get("href") or "")
        text = node.text(strip=True)
        if target and text and target != article.path and target not in links:
            links[target] = text
            if len(links) >= max_links:
                break
    return {
        "headings": headings,
        "links": [{"path": path, "text": text} for path, text in links.items()],
    }
