"""Finding the passages to answer a question from.

The search engine inside a ZIM file is literal: every word of a query must appear in an
article (there is no OR), and results from different books cannot be compared. So a good
answer depends on asking it well, in the right books:

1. **Plan.** The model is shown what the library holds and asked for several short queries
   that come at the subject from different angles, extra keywords a good article would
   contain but the queries do not, and the books most likely to help.
2. **Search the relevant books in depth**, with every query and every keyword.
3. **Search the rest of the library** lightly, in case the answer is somewhere unexpected.
4. **Rank.** Articles found by several searches rise (reciprocal rank fusion); the best are
   read, and scored on how much of the subject and its keywords their text really covers.
"""

import asyncio
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

from .context import Context
from .library import search as zim_search
from .library.article import media, source_url, to_markdown
from .library.reader import read_article
from .providers import AIError, Provider, complete, split_reasoning

log = logging.getLogger(__name__)

MAX_QUERIES = 5
MAX_KEYWORDS = 6
# A keyword search counts for less than a planned query: there are more of them, and each is a
# guess at vocabulary rather than a statement of the subject.
KEYWORD_WEIGHT = 0.35
PLAN_SECONDS = 25  # longer than this and the search goes ahead without the model's plan
MAX_PLANNED_BOOKS = 4  # how many of the model's book picks are trusted
MAX_FOCUS_BOOKS = 8
# A passage must score at least this share of the best one to be kept: a few sources that
# fit beat a full list padded with ones that merely share a word.
KEEP_SHARE = 0.6
MIN_SOURCES = 2
CANDIDATES = 24  # articles read in full before the best are kept
SHORTLIST_PER_BOOK = 6  # of those, at most this many from one book
PER_BOOK = 3  # sources from one book, so a single book cannot crowd out the rest
EXCERPT_CHARS = 2400
RRF_K = 10

STOPWORDS = frozenset(
    "a an and are as at be but by can could did do does for from had has have how i if in is it "
    "its me my of on or should so than that the their them then there these they this to was "
    "we were what when where which who why will with would you your about into over tell "
    "explain describe give show please".split()
)

PLAN_PROMPT = """\
You plan searches of an offline library for a research assistant. The search engine is \
literal: an article is found only if it contains every word of a query, so queries must be \
short.

Given the conversation and the list of books, plan how to find articles that answer the \
user's latest question. Reply in exactly this form and nothing else:

QUERIES:
<3 to 5 search queries, one per line, each 1 to 3 plain keywords>
KEYWORDS: <4 to 6 comma-separated terms>
BOOKS: <1 to 4 book ids from the list, most useful first, comma-separated>

- QUERIES should approach the subject from different angles: the main topic, a likely \
article title, an important sub-topic or method, a related concept. No question words, \
quotes or operators.
- KEYWORDS are important terms a good article on this would contain that are NOT already in \
your queries: synonyms, technical names, specific methods, people, places or parts.
- BOOKS are ids copied exactly from the list. Name a book only if its description fits the \
subject: a specialised book when one fits, an encyclopedia when the question is general. \
Fewer is better; never pad the list with books on other subjects.

If the latest message is a greeting, thanks or small talk that needs no lookup, reply with \
only the word NONE."""

_SMALL_TALK = re.compile(
    r"^(?:(?:hi|hii+|hello|hey|heya|yo|hola|namaste|howdy|greetings|sup)(?: there| again)?"
    r"|good (?:morning|afternoon|evening|night|day)"
    r"|(?:thanks|thank you|thx|ty|cheers)(?: a lot| so much| very much)?"
    r"|ok|okay|k|cool|nice|great|got it|i see|alright|sure|yes|no|yep|nope"
    r"|bye|goodbye|see you|see ya|good ?bye"
    r"|how are you(?: doing)?(?: today)?|what'?s up|who are you|what can you do)$"
)


def is_small_talk(message: str) -> bool:
    """Greetings and acknowledgements: answered directly, with no library search."""
    text = re.sub(r"[^\w\s']", " ", message.lower())
    return bool(_SMALL_TALK.match(" ".join(text.split())))


def keywords(text: str) -> list[str]:
    return [w for w in re.findall(r"\w+", text.lower()) if len(w) > 2 and w not in STOPWORDS]


@dataclass
class Plan:
    """What to search for, and where to look first."""

    queries: list[str] = field(default_factory=list)
    # Terms a good article would contain beyond the queries' own words.
    keywords: list[str] = field(default_factory=list)
    # Ids of the books most likely to hold the answer.
    books: list[str] = field(default_factory=list)
    # Set when the plan was made without the model, and why.
    note: str = ""

    def __bool__(self) -> bool:
        return bool(self.queries)


# -- the library, as the planner sees it ---------------------------------------------------


def scope_books(ctx: Context, book: str, category: str) -> list[dict[str, Any]]:
    """The books a question may draw on: one book, one category, or the whole library."""
    if book:
        row = ctx.book(book)
        if row is None:
            raise AIError(f"No book named {book!r}")
        return [row]
    return ctx.db.all(
        "SELECT * FROM books WHERE error IS NULL AND (? = '' OR category = ?) "
        "ORDER BY article_count DESC",
        (category, category),
    )


def catalog(books: list[dict[str, Any]], limit: int = 7000) -> str:
    """The library as a compact list for the planner: one line per book.

    The hundreds of DevDocs books are folded into one line of topic names, or they would
    drown everything else.
    """
    docs = sorted(
        b["id"].removeprefix("devdocs_en_") for b in books if b["id"].startswith("devdocs_en_")
    )
    lines = []
    for row in books:
        if row["id"].startswith("devdocs_en_"):
            continue
        about = (row["description"] or row["title"]).strip().replace("\n", " ")[:80]
        lines.append(f"{row['id']} — {row['title']}: {about}")
    text = "\n".join(lines)
    if docs:
        text += (
            "\ndevdocs_en_<topic> — DevDocs API and language documentation, one book per "
            "topic: " + ", ".join(docs)
        )
    return text[:limit]


# -- planning --------------------------------------------------------------------------------


def _clean_query(text: str) -> str:
    text = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", text).strip().strip("\"'`.,;:")
    return " ".join(keywords(text)[:4])


def _match_book(name: str, ids: set[str]) -> str | None:
    """A book id from what the model wrote, forgiving near misses."""
    name = name.strip().strip("\"'`.,;:[]()").split(" — ")[0].split(" ")[0]
    if not name:
        return None
    if name in ids:
        return name
    lowered = name.lower()
    for candidate in (f"devdocs_en_{lowered}", lowered):
        if candidate in ids:
            return candidate
    close = sorted(i for i in ids if i.lower().startswith(lowered) or lowered.startswith(i.lower()))
    return close[0] if close and len(lowered) > 3 else None


def parse_plan(reply: str, book_ids: set[str]) -> Plan:
    """Read the planner's reply, tolerating the liberties small models take with the form."""
    text = split_reasoning(reply)[1] or reply
    text = re.sub(r"```\w*|\*\*", "", text)
    if re.fullmatch(r"\W*none\W*", text, flags=re.I):
        return Plan()
    plan = Plan()
    section = "queries"
    for raw in text.splitlines():
        line = raw.strip()
        header = re.match(r"^(queries|keywords|books)\s*[:：-]?\s*(.*)$", line, flags=re.I)
        if header:
            section, line = header.group(1).lower(), header.group(2)
        if not line or line.endswith(":"):
            continue
        if section == "queries":
            for part in [line] if header is None else re.split(r"[,;]", line):
                query = _clean_query(part)
                if query and query not in plan.queries:
                    plan.queries.append(query)
        elif section == "keywords":
            for part in re.split(r"[,;]|\s+-\s+", line):
                term = " ".join(keywords(part)[:3])
                if term and term not in plan.keywords:
                    plan.keywords.append(term)
        else:
            for part in re.split(r"[,;\s]+", line):
                matched = _match_book(part, book_ids)
                if matched and matched not in plan.books:
                    plan.books.append(matched)
    in_queries = {word for query in plan.queries for word in query.split()}
    plan.queries = plan.queries[:MAX_QUERIES]
    # A keyword that only repeats the queries adds nothing.
    plan.keywords = [k for k in plan.keywords if not set(k.split()) <= in_queries][:MAX_KEYWORDS]
    plan.books = plan.books[:MAX_PLANNED_BOOKS]
    return plan


def plan_locally(question: str, history: list[dict[str, str]], note: str = "") -> Plan:
    """A plan taken straight from the question's own words, without asking a model."""
    if is_small_talk(question):
        return Plan()
    words = keywords(question)
    if len(words) < 3:
        # A short follow-up ("and with chlorine?") only makes sense with what came before.
        earlier = [m["content"] for m in history if m["role"] == "user"]
        if earlier:
            words = [*keywords(earlier[-1])[:5], *words]
    words = list(dict.fromkeys(words))[:7]
    if not words:
        return Plan(queries=[question.strip()[:60]], note=note)
    # Every word of a query must match, so offer a narrow and a broad version.
    queries = list(dict.fromkeys([" ".join(words[:3]), " ".join(words[:2])]))
    return Plan(queries=queries, keywords=words[3:], note=note)


async def plan(
    provider: Provider,
    model: str,
    question: str,
    history: list[dict[str, str]],
    books: list[dict[str, Any]],
) -> Plan:
    """Ask the model what to search for and where; fall back to the question's own words."""
    if is_small_talk(question):
        return Plan()
    listing = catalog(books) if len(books) > 1 else ""
    asked = f"{question}\n\nBooks in the library:\n{listing}" if listing else question
    messages = [
        {"role": "system", "content": PLAN_PROMPT},
        *history,
        {"role": "user", "content": asked},
    ]
    try:
        reply = await asyncio.wait_for(complete(provider, model, messages), PLAN_SECONDS)
    except (AIError, TimeoutError) as exc:
        # Planning is a refinement: if the model cannot be asked, search with the question.
        log.info("planning failed, searching with the question's words: %s", exc)
        return plan_locally(question, history, note="Planned without the model.")
    parsed = parse_plan(reply, {b["id"] for b in books})
    if not parsed.queries:
        if re.fullmatch(r"\W*none\W*", split_reasoning(reply)[1] or reply, flags=re.I):
            return Plan()
        return plan_locally(question, history, note="Planned without the model.")
    return parsed


# -- choosing books ----------------------------------------------------------------------------


def route_books(terms: list[str], books: list[dict[str, Any]], limit: int = 4) -> list[str]:
    """Books whose own name or description mentions the subject, best first.

    A cheap complement to the model's choice: "python" finds the Python docs, "ubuntu" finds
    Ask Ubuntu, even when the model did not name them.
    """
    wanted = {t for term in terms for t in term.split() if len(t) > 2}
    scored = []
    for row in books:
        name = set(re.findall(r"[a-z0-9]+", f"{row['id']} {row['title']}".lower()))
        about = set(re.findall(r"[a-z0-9]+", f"{row['description']} {row['tags']}".lower()))
        score = 3 * len(wanted & name) + len(wanted & about)
        if wanted & name:
            scored.append((score, math.log10(1 + row["article_count"]), row["id"]))
    return [book_id for _, _, book_id in sorted(scored, reverse=True)[:limit]]


def choose_focus(
    the_plan: Plan, question: str, books: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The books to search in depth.

    The model's picks, then books named after the subject, and always the largest general
    encyclopedia: it is the likeliest home of an answer the specialised picks miss.
    """
    by_id = {row["id"]: row for row in books}
    if len(books) <= MAX_FOCUS_BOOKS:
        return list(books)
    # Route on the planned queries when the model made a plan: the question's own words
    # include verbs like "make", which is also the name of a book.
    terms = the_plan.queries if the_plan.books else keywords(question)
    picked = [*the_plan.books, *route_books(terms, books, limit=2)]
    encyclopedia = next((row["id"] for row in books if row.get("category") == "wikipedia"), None)
    if encyclopedia:
        picked.append(encyclopedia)
    if not picked:
        # Nothing stands out: start with the largest general-purpose books.
        picked = [row["id"] for row in books if not row["id"].startswith("devdocs_")][:3]
    return [by_id[i] for i in dict.fromkeys(picked) if i in by_id][:MAX_FOCUS_BOOKS]


def search_list(the_plan: Plan) -> list[tuple[str, float]]:
    """Every search to run in the relevant books, with its weight.

    The queries as planned, then each keyword tied to the subject ("chlorine water" rather
    than a bare "chlorine", which on its own finds the element, not the method). A keyword
    search that finds nothing falls back to the keyword alone.
    """
    counts: dict[str, int] = {}
    for query in the_plan.queries:
        for word in query.split():
            counts[word] = counts.get(word, 0) + 1
    subject = max(counts, key=lambda w: counts[w]) if counts and max(counts.values()) > 1 else ""
    searches = [(query, 1.0) for query in the_plan.queries]
    for term in the_plan.keywords:
        tied = f"{term} {subject}" if subject and subject not in term.split() else term
        searches.append((tied, KEYWORD_WEIGHT))
    return list(dict.fromkeys(searches))


# -- searching ---------------------------------------------------------------------------------

Candidates = dict[tuple[str, str], dict[str, Any]]


def search_books(
    ctx: Context,
    books: list[dict[str, Any]],
    queries: list[tuple[str, float]],
    depth: int,
    candidates: Candidates,
    relax: bool = False,
) -> int:
    """Run each ``(query, weight)`` in each book, adding what is found to ``candidates``.

    An article's score is the sum of ``weight / (RRF_K + rank)`` over every search that found
    it, so one that several different searches agree on outranks a single lucky hit. With
    ``relax``, a query that finds nothing is retried with fewer words.
    """
    hits = 0
    for row in books:
        try:
            archive = ctx.pool.get(row["path"])
        except Exception:
            continue
        for query, weight in queries:
            attempts = [query]
            words = query.split()
            if relax and len(words) >= 2:
                attempts.append(" ".join(words[:-1]))  # every word must match, so ask for less
            for attempt_number, attempt in enumerate(attempts):
                try:
                    found = zim_search.search_book(archive, attempt, 0, depth, snippets=False)
                except Exception:
                    break
                if not found["results"]:
                    continue
                factor = weight * (0.5 if found["mode"] != "fulltext" else 1.0)
                factor *= 0.6 if attempt_number else 1.0
                # Coming first in a book of three pages says little; in one of a million, a lot.
                factor *= min(1.0, math.log10(2 + row.get("article_count", 1000)) / 3)
                for rank, hit in enumerate(found["results"]):
                    entry = candidates.setdefault(
                        (row["id"], hit["path"]),
                        {
                            "row": row,
                            "path": hit["path"],
                            "title": hit["title"],
                            "score": 0.0,
                            "found_by": set(),
                        },
                    )
                    entry["score"] += factor / (RRF_K + rank)
                    entry["found_by"].add(query)
                    hits += 1
                break
    return hits


def _passages(
    text: str, terms: set[str], extra: set[str] = frozenset()
) -> tuple[float, float, str]:
    """Cut an article's most relevant passages and score them.

    Returns the share of the subject's ``terms`` those passages contain, the share of the
    ``extra`` keywords, and the passages as an excerpt. Scoring the excerpt rather than the
    whole page matters: a very long page mentions every word somewhere, but only a page that
    is about the subject has them together.
    """
    blocks: list[str] = []
    headings: list[str] = []  # the section each block sits under
    heading = ""
    for block in (b.strip() for b in re.split(r"\n\s*\n", text)):
        if re.match(r"#{1,6} ", block) and "\n" not in block:
            heading = "" if block.startswith("# ") else block
        elif len(block) > 40:
            blocks.append(block)
            headings.append(heading)
    if not blocks:
        return 0.0, 0.0, text[:EXCERPT_CHARS]
    words = [set(re.findall(r"\w+", block.lower())) for block in blocks]
    scored = [
        (len(terms & found) + 0.5 * len(extra & found), -index, index)
        for index, found in enumerate(words)
    ]
    chosen, used = [], 0
    # The opening paragraph defines the topic, so it always goes in.
    for _, _, index in [(0, 0, 0), *sorted(scored, reverse=True)]:
        if index in chosen:
            continue
        block = blocks[index][:1200]
        if used + len(block) > EXCERPT_CHARS and chosen:
            break
        chosen.append(index)
        used += len(block)
    covered = set().union(*(words[i] for i in chosen))
    parts, last = [], ""
    for index in sorted(chosen):
        # Say which section a passage comes from, once per run of passages from it.
        if headings[index] and headings[index] != last:
            parts.append(headings[index])
        last = headings[index]
        parts.append(blocks[index][:1200])
    excerpt = "\n\n".join(parts)
    # Mostly how many of the terms one passage holds together, partly how many the excerpt
    # holds at all: words scattered one per paragraph down a long page count for little.
    together = max(len(terms & found) for found in words) / max(1, len(terms))
    return (
        0.6 * together + 0.4 * len(covered & terms) / max(1, len(terms)),
        len(covered & extra) / max(1, len(extra)),
        excerpt,
    )


_TRANSLATION = re.compile(r"/[a-z]{2}(?:-[a-z]{2,4})?$")


def rank(
    ctx: Context,
    candidates: Candidates,
    the_plan: Plan,
    focus_ids: set[str],
    max_sources: int,
    single_book: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Read the most promising candidates and keep the passages that cover the subject best.

    Returns the chosen passages, best first, and how many articles were read.
    """
    terms = {word for query in the_plan.queries for word in query.split()}
    extra = {word for term in the_plan.keywords for word in term.split()} - terms
    for candidate in candidates.values():
        title_words = set(re.findall(r"\w+", candidate["title"].lower()))
        candidate["score"] *= 1 + 0.5 * len((terms | extra) & title_words) / max(1, len(terms))
        if candidate["row"]["id"] in focus_ids:
            candidate["score"] *= 1.15
        if _TRANSLATION.search(candidate["path"]):
            candidate["score"] *= 0.4  # a translation of an article that is also here in full
    shortlist: list[dict[str, Any]] = []
    taken: dict[str, int] = {}
    for candidate in sorted(candidates.values(), key=lambda c: -c["score"]):
        book_id = candidate["row"]["id"]
        if not single_book and taken.get(book_id, 0) >= SHORTLIST_PER_BOOK:
            continue  # one book must not fill the reading list on its own
        taken[book_id] = taken.get(book_id, 0) + 1
        shortlist.append(candidate)
        if len(shortlist) >= CANDIDATES:
            break
    best = shortlist[0]["score"] if shortlist else 1.0

    read = []
    for candidate in shortlist:
        row = candidate["row"]
        try:
            article = read_article(ctx.pool.get(row["path"]), candidate["path"])
        except Exception:
            continue
        if article is None or not article.mimetype.startswith("text/html"):
            continue
        coverage, extra_coverage, excerpt = _passages(to_markdown(article), terms, extra)
        if not excerpt.strip():
            continue
        # How much of the title is about the subject: "Causes of World War I" is, a long
        # list that happens to mention the same words is not.
        title_words = set(keywords(article.title or candidate["title"]))
        on_topic = len(title_words & (terms | extra)) / max(1, len(title_words))
        final = candidate["score"] / best + 2 * coverage + 0.7 * extra_coverage + 0.8 * on_topic
        read.append(
            {
                "score": final,
                "row": row,
                "path": article.path,
                "title": article.title or candidate["title"],
                "excerpt": excerpt,
                "coverage": coverage,
                "article": article,
            }
        )

    chosen: list[dict[str, Any]] = []
    titles: set[str] = set()
    per_book: dict[str, int] = {}
    ordered = sorted(read, key=lambda r: -r["score"])
    floor = KEEP_SHARE * ordered[0]["score"] if ordered else 0.0
    for item in ordered:
        if item["score"] < floor and len(chosen) >= MIN_SOURCES:
            break
        # One article per title: editions of a book (maxi, nopic) and mirrors of Wikipedia
        # carry the same text, and repeats would crowd out other sources.
        fingerprint = item["title"].strip().lower()
        book_id = item["row"]["id"]
        if fingerprint in titles or (not single_book and per_book.get(book_id, 0) >= PER_BOOK):
            continue
        titles.add(fingerprint)
        per_book[book_id] = per_book.get(book_id, 0) + 1
        chosen.append(item)
        if len(chosen) >= max_sources:
            break
    for item in chosen:
        try:
            article = item.pop("article")
            item["media"] = media(article, item["row"]["id"])
            item["original"] = source_url(article)
        except Exception:
            item["media"] = []
    return chosen, len(read)
