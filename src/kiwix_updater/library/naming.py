import re

_DATE_SUFFIX = re.compile(r"_(\d{4}-\d{2}(?:-\d{2})?)$")


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
