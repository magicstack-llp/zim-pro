import posixpath
import re
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from markdownify import markdownify
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .reader import CAPTIONS, Article, clean_tree


def resolve_link(article_path: str, href: str) -> str | None:
    """Turn a link found in an article into the entry path it points to."""
    parts = urlsplit(href)
    if parts.scheme or parts.netloc or not parts.path:
        return None
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(article_path), parts.path))
    return None if joined.startswith("..") else unquote(joined)


# Sections that hold no prose: lists of links and citations.
_LINK_SECTIONS = frozenset(
    "references citations footnotes bibliography sources".split()
    + ["works cited", "further reading", "external links", "see also", "notes and references"]
)
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_LICENCE = re.compile(r"^This article is issued from .*", re.M | re.S)


def markdown_url(book_id: str, path: str) -> str:
    return f"/markdown/{quote(book_id)}/{quote(path, safe='/:@!$&()*+,;=~')}"


def content_url(book_id: str, path: str) -> str:
    return f"/content/{quote(book_id)}/{quote(path, safe='/:@!$&()*+,;=~')}"


_DECORATION = re.compile(
    r"icon|logo|flag|sprite|badge|avatar|emoji|spacer|pixel|button|small", re.I
)
SMALLEST_PICTURE = 80  # pixels; anything narrower is an icon


def _size(node: Any, name: str) -> int | None:
    digits = re.match(r"\d+", node.attributes.get(name) or "")
    return int(digits.group()) if digits else None


def _picture_path(node: Any, article_path: str) -> str | None:
    """The entry an <img> shows, or None when it is decoration rather than content."""
    src = node.attributes.get("src") or node.attributes.get("data-src") or ""
    target = resolve_link(article_path, src)
    if target is None:
        return None
    width, height = _size(node, "width"), _size(node, "height")
    if any(side is not None and side < SMALLEST_PICTURE for side in (width, height)):
        return None
    marks = " ".join([target.rsplit("/", 1)[-1], node.attributes.get("class") or ""])
    if width is None and (_DECORATION.search(marks) or target.endswith(".svg")):
        return None
    return target


def _caption(node: Any) -> str:
    """The caption printed beside a picture or video, or failing that its alt text."""
    text = node.attributes.get("alt") or node.attributes.get("title") or ""
    if re.fullmatch(r"\S{12,}", text.strip()):
        text = ""  # a file name or an id, not a description
    parent = node.parent
    for _ in range(3):
        if parent is None or parent.tag in ("body", "html"):
            break
        found = parent.css_first(CAPTIONS)
        if found is not None:
            text = found.text(separator=" ", strip=True)
            break
        if len(parent.css("img, video, audio")) > 1:
            break  # further up, a caption would belong to some other picture
        parent = parent.parent
    return re.sub(r"\s+([.,;:!?)])", r"\1", " ".join(text.split()))[:300]


def media(article: Article, book_id: str, limit: int = 8) -> list[dict[str, str]]:
    """The pictures, videos and recordings in an article, in page order."""
    if not article.mimetype.startswith("text/html"):
        return []
    tree = clean_tree(article.content, keep_captions=True)
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for node in tree.css("img, video, audio"):
        if node.tag == "img":
            if node.parent is not None and node.parent.tag in ("video", "audio", "picture"):
                continue
            target = _picture_path(node, article.path)
            item = {"kind": "image"}
        else:
            source = node.css_first("source[src]")
            src = node.attributes.get("src") or (source.attributes.get("src") if source else "")
            target = resolve_link(article.path, src or "")
            item = {"kind": node.tag}
            poster = resolve_link(article.path, node.attributes.get("poster") or "")
            if poster:
                item["poster"] = content_url(book_id, poster)
        if target is None or target in seen:
            continue
        seen.add(target)
        item["url"] = content_url(book_id, target)
        item["caption"] = _caption(node)
        found.append(item)
        if len(found) >= limit:
            break
    return found


def _place_media(tree: Any, article_path: str, book_id: str) -> None:
    """Keep an article's pictures in its Markdown page, and link its videos.

    A picture takes its caption as its description, so the two stay together; captions
    left without a picture are dropped.
    """
    for node in tree.css("video, audio"):
        source = node.css_first("source[src]")
        src = node.attributes.get("src") or (source.attributes.get("src") if source else "")
        target = resolve_link(article_path, src or "")
        if target is None:
            node.decompose()
            continue
        label = "Video" if node.tag == "video" else "Recording"
        name = unquote(target.rsplit("/", 1)[-1])
        link = HTMLParser(f'<p><a href="{content_url(book_id, target)}">{label}: {name}</a></p>')
        node.replace_with(link.css_first("p"))
    for node in tree.css("img"):
        target = _picture_path(node, article_path)
        if target is None:
            node.decompose()
            continue
        caption = _caption(node).replace("[", "(").replace("]", ")")
        for name in list(node.attributes):
            del node.attrs[name]
        node.attrs["src"] = content_url(book_id, target)
        node.attrs["alt"] = caption
    for node in tree.css(CAPTIONS):
        node.decompose()
    for node in tree.css("picture > source, object, embed, canvas"):
        node.decompose()


def _link_articles(tree: Any, article_path: str, book_id: str) -> None:
    """Point links between articles at the Markdown page of their target."""
    for node in tree.css("a"):
        href = node.attributes.get("href") or ""
        target = resolve_link(article_path, href)
        if node.css_first("img") is not None or not node.text(strip=True):
            node.unwrap()  # a link around a picture
        elif target is None and not href.startswith("http"):
            node.unwrap()  # an anchor within the page
        elif target is not None:
            node.attrs["href"] = markdown_url(book_id, target)
        for name in ("title", "class", "id"):
            if name in node.attributes:
                del node.attrs[name]


def _prune_sections(text: str, drop: frozenset[str]) -> str:
    """Drop the sections named in ``drop``, and headings left with nothing under them."""
    kept: list[str] = []
    skip_level = 0
    fenced = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        match = None if fenced else _HEADING.match(line)
        if match:
            level = len(match.group(1))
            if skip_level and level > skip_level:
                continue
            skip_level = 0
            # A heading that follows a heading of the same or a deeper level closes a
            # section that had no text in it.
            while kept and (not kept[-1].strip() or _is_empty_heading(kept[-1], level)):
                kept.pop()
            if re.sub(r"[^\w ]", "", match.group(2)).strip().lower() in drop:
                skip_level = level
                continue
            kept.append("")
        elif skip_level:
            continue
        kept.append(line)
    while kept and (not kept[-1].strip() or _HEADING.match(kept[-1])):
        kept.pop()
    return "\n".join(kept)


def _is_empty_heading(line: str, next_level: int) -> bool:
    match = _HEADING.match(line)
    return bool(match) and len(match.group(1)) >= next_level


def _tidy(text: str) -> str:
    text = _LICENCE.sub("", text)
    text = re.sub(r"\*\*([.,;:]?)\*\*", r"\1", text)  # bold around nothing
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def to_markdown(article: Article, book_id: str = "") -> str:
    """Convert an article to Markdown, keeping only its readable content.

    Without ``book_id`` the result is for a model to read: links are flattened to their
    text and the sections that only list links are left out, so no tokens go on them. With
    it the result is a page for a person: pictures are kept, and links between articles
    lead to the Markdown page of the article they point to.
    """
    if not article.mimetype.startswith("text/html"):
        if article.mimetype.startswith("text/"):
            return article.content.decode("utf-8", "replace")
        return f"[{article.mimetype} content, {len(article.content)} bytes]"
    tree = clean_tree(article.content, keep_captions=bool(book_id))
    if book_id:
        _link_articles(tree, article.path, book_id)
        _place_media(tree, article.path, book_id)
    else:
        for node in tree.css("img, picture, video, audio, object, embed, canvas"):
            node.decompose()
    root = tree.body or tree.root
    if root is None:
        return ""
    options: dict[str, Any] = {} if book_id else {"strip": ["a"]}
    text = markdownify(
        root.html or "",
        heading_style="ATX",
        bullets="-",
        keep_inline_images_in=["td", "th", "li", "a", "div", "span", "p", "figure"],
        **options,
    )
    text = _prune_sections(_tidy(text), frozenset() if book_id else _LINK_SECTIONS)
    title = (article.title or "").strip()
    if title and not re.search(r"^# ", text, re.M):
        text = f"# {title}\n\n{text}"
    return text.strip()


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
