import re

_DATE_SUFFIX = re.compile(r"_(\d{4}-\d{2}(?:-\d{2})?)[a-z]?$")


_TAG_CATEGORY = re.compile(r"(?:^|;)_category:([^;]+)")
_STACK_EXCHANGE = re.compile(
    r"stackexchange\.com|stackoverflow\.com|^(askubuntu|superuser|serverfault|mathoverflow)\."
)


def category_of(book_id: str, tags: str) -> str:
    """The Kiwix category of a book.

    Publishers tag most books with ``_category:<name>``. Many are not tagged at all (every
    DevDocs book, for one), so those fall back to what the file name gives away.
    """
    match = _TAG_CATEGORY.search(tags)
    if match:
        return match.group(1).strip()
    if book_id.startswith("devdocs_"):
        return "devdocs"
    if _STACK_EXCHANGE.search(book_id):
        return "stack_exchange"
    for prefix in ("wikipedia", "wiktionary", "wikibooks", "wikiquote", "wikisource",
                   "wikivoyage", "wikiversity", "gutenberg", "phet", "ted", "ifixit"):  # fmt: skip
        if book_id.startswith(prefix + "_"):
            return "iFixit" if prefix == "ifixit" else prefix
    return "other"


def split_filename(filename: str) -> tuple[str, str]:
    """Split ``wikipedia_en_all_maxi_2024-01.zim`` into its stem and date.

    The stem (``wikipedia_en_all_maxi``) is stable across releases of a book
    and is what local files are matched against the catalog with.
    """
    base = filename.rsplit("/", 1)[-1]
    for suffix in (".meta4", ".part", ".zim"):
        base = base.removesuffix(suffix)
    match = _DATE_SUFFIX.search(base)
    if not match:
        return base, ""
    return base[: match.start()], match.group(1)
