"""Turning a fetched web page into a page of a ZIM.

Two passes. :func:`prepare` runs when a page is fetched: it cleans the page, makes every
address absolute and lists what the page links to and embeds. :func:`localise` runs once the
crawl is over and it is known what was saved: addresses of saved pages and files become
relative paths inside the ZIM, and everything else keeps pointing at the web.
"""

import hashlib
import posixpath
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

_ALWAYS_DROP = "script, noscript, template, iframe, object, embed, base, link[rel~=preload]"
_CHROME = "nav, aside, form, dialog, button, input, select, textarea"
# Boxes that are about the site, not the page. Matched on whole words of class and id.
_NOISE = re.compile(
    r"(?:^|[\s_-])(cookie|consent|gdpr|advert|adsense|ad-slot|popup|modal|newsletter|"
    r"subscribe|share|sharing|social|sidebar|breadcrumbs?|skip-link|site-header|"
    r"site-footer|site-nav|navbar|menubar|(?:main|sub|search|nav)?menu|topbar|masthead|"
    r"toolbar|tagline|"
    r"interlanguage|portlet|editsection|navbox|catlinks|printfooter|noprint)(?:$|[\s_-])",
    re.I,
)
_MAIN = (
    "article, main, [role=main], [itemprop=articleBody], #content, #main, #main-content, "
    ".content, .main-content, .post-content, .entry-content, .article-content, .post, "
    ".markdown-body, .mw-parser-output, .documentation, .doc-content, .page-content"
)
_TRACKING = re.compile(r"^(utm_\w+|fbclid|gclid|mc_[ce]id|ref|ref_src|igshid|yclid|_hs\w+)$")
# Much less text than the page had means the picker chose the wrong box.
KEEP_SHARE = 0.4

PAGE_CSS = """
body{font:17px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#1c1c1e;
background:#fff;max-width:46rem;margin:0 auto;padding:1.5rem 1rem 4rem}
img,video{max-width:100%;height:auto}pre{overflow:auto;background:#f4f4f5;padding:.8rem;
border-radius:.5rem}code{font-size:.9em}table{border-collapse:collapse;display:block;
overflow:auto}td,th{border:1px solid #d4d4d8;padding:.3rem .6rem}a{color:#0f766e}
blockquote{border-left:3px solid #d4d4d8;margin-left:0;padding-left:1rem;color:#52525b}
h1{line-height:1.25}.zp-source{margin-top:3rem;padding-top:1rem;border-top:1px solid #e4e4e7;
font-size:.85rem;color:#71717a;word-break:break-all}
@media(prefers-color-scheme:dark){body{background:#18181b;color:#e4e4e7}pre{background:#27272a}
td,th{border-color:#3f3f46}a{color:#5eead4}}
"""


def normalise(url: str, base: str = "") -> str:
    """One spelling for each address: absolute, no fragment, no tracking parameters."""
    parts = urlsplit(urljoin(base, url.strip()))
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    query = [(k, v) for k, v in query if not _TRACKING.match(k)]
    path = re.sub(r"/{2,}", "/", parts.path) or "/"
    host = parts.netloc.lower()
    host = host.removesuffix(":80") if parts.scheme == "http" else host.removesuffix(":443")
    return urlunsplit((parts.scheme, host, path, urlencode(query), ""))


def zim_path(url: str) -> str:
    """Where an address is kept inside the ZIM: its host and path, readable in the reader."""
    parts = urlsplit(url)
    path = unquote(parts.path)
    if path.endswith("/") or not path:
        path += "index"
    if parts.query:
        # Different queries are different pages; a short hash keeps them apart.
        stem, dot, extension = path.rpartition(".")
        mark = "_" + hashlib.sha1(parts.query.encode()).hexdigest()[:8]
        path = f"{stem}{mark}.{extension}" if dot and "/" not in extension else path + mark
    return (parts.netloc + path).strip("/")


def relative(from_path: str, to_path: str) -> str:
    """The link from one ZIM path to another, as a browser will resolve it."""
    href = posixpath.relpath(to_path, posixpath.dirname(from_path) or ".")
    return quote(href, safe="/@!$&()*+,;=~.-_")


@dataclass
class Prepared:
    title: str
    html: str
    text: str
    language: str = ""
    canonical: str = ""
    description: str = ""
    links: list[str] = field(default_factory=list)  # pages this one points to
    resources: list[tuple[str, str]] = field(default_factory=list)  # (address, kind)
    icon: str = ""
    # Every icon the page names for itself, the most fitting first; and the picture it
    # gives to be shown where it is shared, which will do when there is no icon.
    icons: list[str] = field(default_factory=list)
    share_picture: str = ""
    fell_back: bool = False  # the cleaned article was too thin, so the whole body was kept


def _text(node) -> str:
    return " ".join(node.text(separator=" ", strip=True).split()) if node is not None else ""


def _drop_noise(tree: HTMLParser, clean: bool) -> None:
    for node in tree.css(_ALWAYS_DROP):
        node.decompose()
    for node in tree.css("*"):
        for name in [a for a in node.attributes if a.startswith("on")]:
            del node.attrs[name]  # inline scripts: onclick and the like
    if not clean:
        return
    for node in tree.css(f"{_CHROME}, style, link"):
        node.decompose()
    for node in tree.css("[class], [id]"):
        marks = f"{node.attributes.get('class') or ''} {node.attributes.get('id') or ''}"
        if node.tag not in ("html", "body", "main", "article") and _NOISE.search(marks):
            node.decompose()
    # A header or footer that belongs to the page wraps it; one inside an article is its own.
    for node in tree.css("body > header, body > footer, body > div > footer, [role=banner]"):
        node.decompose()
    wiki = ".mw-jump-link, .mw-indicators, #siteNotice, .vector-column-end, .vector-dropdown"
    for node in tree.css(f"[role=contentinfo], [role=navigation], [aria-hidden=true], {wiki}"):
        node.decompose()


def _pick_main(tree: HTMLParser):
    """The box that holds the article: the candidate with the most text."""
    best, best_size = None, 0
    for node in tree.css(_MAIN):
        size = len(_text(node))
        if size > best_size:
            best, best_size = node, size
    return best, best_size


def _best_image(node, base: str) -> str:
    """The address of the picture an <img> shows, preferring its largest version."""
    attributes = node.attributes
    candidates = []
    for name in ("srcset", "data-srcset"):
        for part in (attributes.get(name) or "").split(","):
            bits = part.split()
            if not bits:
                continue
            size = re.sub(r"[^\d.]", "", bits[1]) if len(bits) > 1 else ""
            candidates.append((float(size or 1), bits[0]))
    src = ""
    for name in ("data-src", "data-original", "data-lazy-src", "src"):
        value = (attributes.get(name) or "").strip()
        if value and not value.startswith("data:"):
            src = value
            break
    if candidates:
        src = max(candidates)[1]  # the sharpest version the page offers
    return normalise(src, base) if src else ""


def _icons(tree: HTMLParser, base: str) -> list[str]:
    """The pictures a page names for itself: touch icons, then icons.

    Larger and plainer first: a touch icon is a square made to be shown on its own. Vector
    icons come last, as they cannot be drawn here.
    """

    def size(node) -> int:
        match = re.match(r"(\d+)", node.attributes.get("sizes") or "")
        return int(match.group(1)) if match else 0

    ranked: list[tuple[int, int, str]] = []
    for order, selector in enumerate(
        ("link[rel~='apple-touch-icon'][href], link[rel~='apple-touch-icon-precomposed'][href]",
         "link[rel~=icon][href]")
    ):  # fmt: skip
        for node in tree.css(selector):
            address = normalise(node.attributes.get("href") or "", base)
            if not address:
                continue
            vector = address.split("?")[0].lower().endswith(".svg") or "svg" in (
                node.attributes.get("type") or ""
            )
            ranked.append((2 if vector else order, -size(node), address))
    found: list[str] = []
    for _, _, address in sorted(ranked, key=lambda item: item[:2]):
        if address not in found:
            found.append(address)
    return found


def prepare(
    html: bytes | str,
    url: str,
    clean: bool = True,
    images: bool = True,
    video: bool = True,
) -> Prepared:
    """Clean a fetched page and list what it links to and embeds."""
    tree = HTMLParser(html)
    head = tree.head
    title = _text(tree.css_first("title"))
    base_node = tree.css_first("base[href]")
    base = normalise(base_node.attributes.get("href") or "", url) if base_node else url
    canonical_node = tree.css_first("link[rel=canonical][href]")
    canonical = normalise(canonical_node.attributes["href"], base) if canonical_node else ""
    language = (
        (tree.css_first("html").attributes.get("lang") or "") if tree.css_first("html") else ""
    )
    icons = _icons(tree, base)
    icon = icons[0] if icons else ""
    shared = tree.css_first(
        "meta[property='og:image'][content], meta[name='twitter:image'][content]"
    )
    share_picture = normalise(shared.attributes.get("content") or "", base) if shared else ""
    heading = _text(tree.css_first("h1"))
    described = tree.css_first("meta[name=description][content], meta[property='og:description']")
    description = " ".join((described.attributes.get("content") or "").split()) if described else ""
    styles = []
    if not clean and head is not None:
        for node in head.css("link[rel~=stylesheet][href]"):
            address = normalise(node.attributes["href"], base)
            if address:
                styles.append(address)
        inline = [node.text() for node in head.css("style")]
    else:
        inline = []

    _drop_noise(tree, clean)
    body = tree.body
    if body is None:
        return Prepared(title or url, "", "", language, canonical)
    whole = len(_text(body))
    content, fell_back = body, False
    if clean:
        picked, size = _pick_main(tree)
        if picked is not None and size >= KEEP_SHARE * whole:
            content = picked
        elif picked is not None and whole > 400:
            fell_back = True  # keep everything rather than lose text to a bad guess

    result = Prepared(title or heading or url, "", "", language, canonical, description, icon=icon)
    result.icons = icons
    result.share_picture = share_picture
    result.fell_back = fell_back
    for address in styles:
        result.resources.append((address, "style"))

    for node in content.css("picture > source, img[src^='data:image/gif']"):
        node.decompose()
    for node in content.css("img"):
        address = _best_image(node, base)
        alt = (node.attributes.get("alt") or "").strip()
        if not address or not images:
            if alt:
                node.replace_with(f"[{alt}]")
            else:
                node.decompose()
            continue
        for name in list(node.attributes):
            if name not in ("alt", "width", "height", "title"):
                del node.attrs[name]
        node.attrs["src"] = address
        node.attrs["loading"] = "lazy"
        result.resources.append((address, "image"))
    for node in content.css("video, audio"):
        source = node.css_first("source[src]")
        address = normalise(
            node.attributes.get("src") or (source.attributes.get("src") if source else "") or "",
            base,
        )
        if not address or not video:
            label = "Video" if node.tag == "video" else "Recording"
            replacement = HTMLParser(
                f'<p><a href="{address}">{label} (not included): {address}</a></p>'
                if address
                else "<p></p>"
            )
            node.replace_with(replacement.css_first("p"))
            continue
        poster = normalise(node.attributes.get("poster") or "", base)
        for child in node.css("source, track"):
            child.decompose()
        for name in list(node.attributes):
            del node.attrs[name]
        node.attrs["src"] = address
        node.attrs["controls"] = ""
        node.attrs["preload"] = "metadata"
        result.resources.append((address, "media"))
        if poster and images:
            node.attrs["poster"] = poster
            result.resources.append((poster, "image"))
    for node in content.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if href.startswith("#"):
            continue  # a jump within the page works as it is
        address = normalise(href, base)
        if not address:
            if not href.startswith(("mailto:", "tel:")):
                node.unwrap()
            continue
        fragment = urlsplit(urljoin(base, href)).fragment
        node.attrs["href"] = address + (f"#{fragment}" if fragment else "")
        for name in ("target", "rel", "ping", "download"):
            if name in node.attributes:
                del node.attrs[name]
        result.links.append(address)

    result.text = _text(content)
    inner = content.html or ""
    if content.tag == "body":
        inner = re.sub(r"^<body[^>]*>|</body>$", "", inner.strip())
    shown_title = "" if (heading or not clean) else f"<h1>{_escape(result.title)}</h1>"
    style_tags = "".join(f'<link rel="stylesheet" href="{a}">' for a in styles)
    style_tags += "".join(f"<style>{css}</style>" for css in inline)
    result.html = (
        '<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>{title}</title>{canonical}{styles}</head><body>{heading}{body}"
        '<p class="zp-source">Saved from <a href="{url}">{url}</a></p></body></html>'
    ).format(
        lang=_escape(language or "en"),
        title=_escape(result.title),
        canonical=f'<link rel="canonical" href="{_escape(url)}">',
        styles=f"<style>{PAGE_CSS}</style>" if clean else style_tags,
        heading=shown_title,
        body=inner,
        url=_escape(url),
    )
    return result


def serialise(tree: HTMLParser) -> str:
    """A parsed page as HTML text, with exactly one doctype at the top."""
    html = (tree.html or "").lstrip()
    return html if html[:9].lower() == "<!doctype" else "<!doctype html>" + html


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


_CSS_URL = re.compile(r"url\(\s*(['\"]?)([^)'\"]+)\1\s*\)")


def css_resources(css: str, url: str) -> list[str]:
    """The files a stylesheet pulls in: fonts and background pictures."""
    found = []
    for match in _CSS_URL.finditer(css):
        address = normalise(match.group(2), url) if not match.group(2).startswith("data:") else ""
        if address:
            found.append(address)
    return found


def localise_css(css: str, url: str, path: str, saved: dict[str, str]) -> str:
    def swap(match: re.Match[str]) -> str:
        address = normalise(match.group(2), url) if not match.group(2).startswith("data:") else ""
        if address in saved:
            return f"url({relative(path, saved[address])})"
        return match.group(0)

    return _CSS_URL.sub(swap, css)


def localise(html: str, path: str, saved: dict[str, str]) -> str:
    """Point a prepared page at what was saved: ``saved`` maps addresses to ZIM paths."""
    tree = HTMLParser(html)
    for node in tree.css("a[href]"):
        href = node.attributes.get("href") or ""
        address, _, fragment = href.partition("#")
        if address in saved:
            node.attrs["href"] = relative(path, saved[address]) + (
                f"#{fragment}" if fragment else ""
            )
        elif address.startswith(("http://", "https://")):
            node.attrs["rel"] = "noopener"
    for node in tree.css("img[src]"):
        address = node.attributes.get("src") or ""
        if address in saved:
            node.attrs["src"] = relative(path, saved[address])
        else:
            # The picture could not be saved: its description is better than a broken box.
            alt = (node.attributes.get("alt") or "").strip()
            if alt:
                node.replace_with(f"[{alt}]")
            else:
                node.decompose()
    for node in tree.css("video[src], audio[src]"):
        address = node.attributes.get("src") or ""
        poster = node.attributes.get("poster") or ""
        if poster:
            if poster in saved:
                node.attrs["poster"] = relative(path, saved[poster])
            else:
                del node.attrs["poster"]
        if address in saved:
            node.attrs["src"] = relative(path, saved[address])
        else:
            link = HTMLParser(f'<p><a href="{address}">Media (not saved): {address}</a></p>')
            node.replace_with(link.css_first("p"))
    for node in tree.css("link[rel=stylesheet][href]"):
        address = node.attributes.get("href") or ""
        if address in saved:
            node.attrs["href"] = relative(path, saved[address])
        else:
            node.decompose()
    return serialise(tree)
