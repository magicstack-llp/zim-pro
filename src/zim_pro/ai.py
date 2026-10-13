"""Ask AI: the prompts, sources and messages around an answer (see retrieval.py for search).

The flow is retrieval-augmented rather than tool-calling, because small local models
are unreliable at calling tools: the model first turns the question into keyword
searches, the library is searched, the best passages are put in the prompt, and the
answer is streamed back with numbered citations.
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .providers import AIError, split_reasoning
from .retrieval import is_small_talk, keywords

__all__ = ["AIError", "Source", "is_small_talk", "keywords"]


HISTORY_TURNS = 6

CHAT_PROMPT = """\
You are the assistant built into the user's offline library (Wikipedia, Stack Exchange, \
software documentation and similar). The user's message needed no library search. Reply \
briefly and naturally. If they ask what you can do, say you answer questions from the \
books in their library and cite the articles you used."""

ANSWER_PROMPT = """\
You are a research assistant answering questions from the user's offline library.
Numbered sources follow the question: articles from the library, and web pages when the \
library was not enough. Base your answer on them and cite the ones you use inline as [1], \
[2]. If the sources do not contain the answer, say so plainly, then answer from your own \
knowledge and say that you are doing so. Be direct and well organised, and use Markdown."""

WEB_PROMPT = """\
You are a research assistant. The user's offline library had nothing useful for this \
question, so answer it by searching the web. Be direct and well organised, use Markdown, \
and cite the pages you rely on."""

FORMAT_RULES = """

Formatting rules:
- Write GitHub-flavoured Markdown. Never use HTML tags.
- Start with the answer itself. No preamble such as "Answer:" and do not repeat the question.
- Keep paragraphs short. Leave a blank line between paragraphs, lists, tables and code blocks.
- For steps or several items use "-" bullets or "1." numbered lists, one item per line.
- Use "## " headings only when the answer has several sections, and never a "# " title.
- Put code and commands in fenced blocks with the language named, and always close the fence.
- Use a table only to compare things, with a header row and a |---| row under it.
- Use **bold** sparingly, for key terms only.
- Do not wrap the whole answer in a code block, and do not show your reasoning."""

CITATION_RULES = """
- Cite as [1], [2] right after the sentence the source supports: one number per bracket, \
never [1, 2], footnotes or "Source 1".
- Do not list the sources at the end; they are shown next to your answer."""

# Below this share of matched search terms, the library is treated as not having an answer.
ENOUGH_COVERAGE = 0.5


@dataclass
class Source:
    """A passage the answer may cite: an article from the library, or a web page."""

    n: int
    kind: str  # "library" or "web"
    title: str
    excerpt: str
    book: str = ""
    book_title: str = ""
    path: str = ""
    url: str = ""
    site: str = ""
    # Share of the search terms the article contains; low means a weak match.
    coverage: float = 0.0
    # Pictures, videos and recordings on the page, shown beside the source.
    media: list[dict[str, str]] = field(default_factory=list)
    # The web address the article was saved from, for books made from links.
    original: str = ""


def system_prompt(base: str, citing: bool, instructions: str = "") -> str:
    """A system prompt with the formatting rules and the user's own instructions added."""
    prompt = base + FORMAT_RULES + (CITATION_RULES if citing else "")
    if instructions.strip():
        prompt += f"\n\nThe user has also asked for this in every answer:\n{instructions.strip()}"
    return prompt


def build_messages(
    question: str,
    history: list[dict[str, str]],
    sources: list[Source] | None,
    instructions: str = "",
) -> list[dict[str, str]]:
    """The prompt for the answer; ``sources`` is None when no search was made."""
    if sources is None:
        return [
            {"role": "system", "content": system_prompt(CHAT_PROMPT, False, instructions)},
            *history,
            {"role": "user", "content": question},
        ]
    if sources:
        listing = "\n\n".join(
            f"[{s.n}] {s.title} — {s.book_title or s.site}\n{s.excerpt}" for s in sources
        )
    else:
        listing = "(The search found nothing relevant.)"
    return [
        {"role": "system", "content": system_prompt(ANSWER_PROMPT, True, instructions)},
        *history,
        {"role": "user", "content": f"{question}\n\nSources:\n\n{listing}"},
    ]


def clean_history(history: list[dict[str, Any]]) -> list[dict[str, str]]:
    turns = [
        # Answers stored before thinking was separated may still carry it inline.
        {"role": m["role"], "content": split_reasoning(str(m["content"]))[1][:6000]}
        for m in history
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    return turns[-HISTORY_TURNS * 2 :]


def library_sources(passages: list[dict[str, Any]]) -> list[Source]:
    """Numbered sources from the passages the search kept."""
    return [
        Source(
            n=index,
            kind="library",
            title=item["title"],
            excerpt=item["excerpt"],
            book=item["row"]["id"],
            book_title=item["row"]["title"],
            path=item["path"],
            coverage=item["coverage"],
            media=item.get("media", []),
            original=item.get("original", ""),
        )
        for index, item in enumerate(passages, 1)
    ]


def library_is_enough(sources: list[Source]) -> bool:
    """Whether the library search found something that plausibly answers the question."""
    return any(s.kind == "library" and s.coverage >= ENOUGH_COVERAGE for s in sources)


def web_sources(hits: list[dict[str, str]], start: int) -> list[Source]:
    sources = []
    for index, hit in enumerate(hits):
        site = hit.get("site") or urlsplit(hit["url"]).netloc.removeprefix("www.")
        sources.append(
            Source(
                n=start + index,
                kind="web",
                title=hit.get("title") or site,
                excerpt=hit.get("text", ""),
                url=hit["url"],
                site=site,
            )
        )
    return sources


def web_messages(
    question: str, history: list[dict[str, str]], instructions: str = ""
) -> list[dict[str, str]]:
    """The prompt for a model that searches the web itself."""
    return [
        {"role": "system", "content": system_prompt(WEB_PROMPT, False, instructions)},
        *history,
        {"role": "user", "content": question},
    ]


def public_source(source: Source) -> dict[str, Any]:
    data = asdict(source)
    del data["coverage"]
    # For display only: drop Markdown markup and the title the excerpt usually opens with.
    plain = re.sub(r"[*_`#>|]+|-{3,}|!?\[([^\]]*)\]\([^)]*\)", r"\1", source.excerpt)
    plain = " ".join(plain.split()).removeprefix(source.title).strip()
    data["excerpt"] = plain[:240]
    return data
