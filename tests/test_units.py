from pathlib import Path

import pytest

from zim_pro.catalog import opds
from zim_pro.catalog.updates import book_updates
from zim_pro.db import Database
from zim_pro.downloads import metalink
from zim_pro.downloads.engine import Bitmap
from zim_pro.library.article import resolve_link
from zim_pro.library.naming import category_of, split_filename

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("wikipedia_en_all_maxi_2024-01.zim", ("wikipedia_en_all_maxi", "2024-01")),
        ("100r.co_en_all_2025-03.zim.meta4", ("100r.co_en_all", "2025-03")),
        (
            "https://x.org/zim/devdocs/devdocs_en_bash_2026-10.zim.meta4",
            ("devdocs_en_bash", "2026-10"),
        ),
        ("alexandria.dk_en_all_2026-09a.zim", ("alexandria.dk_en_all", "2026-09")),
        ("custom.zim", ("custom", "")),
    ],
)
def test_split_filename(filename, expected):
    assert split_filename(filename) == expected


@pytest.mark.parametrize(
    ("book_id", "tags", "expected"),
    [
        ("wikipedia_en_all_maxi", "wikipedia;_category:wikipedia;_ftindex:yes", "wikipedia"),
        ("100r.co_en_all", "preppers;_ftindex:yes;_category:other", "other"),
        ("devdocs_en_bash", "devdocs;bash", "devdocs"),
        ("askubuntu.com_en_all", "", "stack_exchange"),
        ("health.stackexchange.com_en_all", "", "stack_exchange"),
        ("ifixit_en_all", "", "iFixit"),
        ("archlinux_en_all_maxi", "Linux distro;archlinux", "other"),
    ],
)
def test_category_of(book_id, tags, expected):
    assert category_of(book_id, tags) == expected


def test_metalink_parse():
    link = metalink.parse((FIXTURES / "bash.meta4").read_bytes())
    assert link.name == "devdocs_en_bash_2026-10.zim"
    assert link.size == 572117
    assert len(link.sha256) == 64
    assert link.piece_length == 4194304
    assert len(link.piece_hashes) == link.piece_count == 1
    assert link.piece_range(0) == (0, 572117)
    assert link.urls[0].startswith("https://") and len(link.urls) >= 2


def test_metalink_rejects_missing_mirrors():
    xml = (
        b'<metalink xmlns="urn:ietf:params:xml:ns:metalink">'
        b'<file name="a"><size>5</size></file></metalink>'
    )
    with pytest.raises(ValueError):
        metalink.parse(xml)


def test_bitmap_roundtrip():
    bitmap = Bitmap(11)
    bitmap.set(0)
    bitmap.set(10)
    assert bitmap.missing() == list(range(1, 10))
    again = Bitmap(11, bitmap.to_bytes())
    assert again[0] and again[10] and not again[5]
    assert Bitmap(12, b"\xff").done_count() == 0  # wrong length is ignored


def test_resolve_link():
    assert resolve_link("A/Linux", "Kernel") == "A/Kernel"
    assert resolve_link("A/Linux", "../I/logo.png") == "I/logo.png"
    assert resolve_link("A/Linux", "https://example.org/") is None
    assert resolve_link("A/Linux", "#history") is None
    assert resolve_link("A/Linux", "../../etc") is None


def _book(db, **fields):
    row = {"id": "b", "path": "/x/b.zim", "filename": "b.zim", "size": 1, "mtime": 0,
           "name": "b", "flavour": "", "date": "2025-01-09", "uuid": "old"}  # fmt: skip
    row.update(fields)
    db.execute(
        f"INSERT INTO books ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
        list(row.values()),
    )


def test_opds_parse_and_update_matching():
    rows = opds.parse_feed(
        (FIXTURES / "feed.xml").read_bytes(), "https://library.kiwix.org/catalog/v2/entries"
    )
    assert len(rows) == 1
    entry = rows[0]
    assert entry["stem"] == "devdocs_en_bash" and entry["issued"].startswith("2026-10-05")
    assert entry["url"].endswith(".zim.meta4") and entry["size"] == 572416
    assert entry["illustration"].startswith("https://library.kiwix.org/catalog/v2/illustration/")

    db = Database(":memory:")
    opds.store(db, rows)
    _book(db, id="devdocs_en_bash", path="/x/a.zim")
    _book(db, id="gone_en_all", path="/x/g.zim", name="gone_en_all")
    _book(
        db,
        id="renamed",
        path="/x/r.zim",
        name="devdocs_en_bash",
        uuid=entry["uuid"],
        date="2026-10-05",
    )
    status = {item["book"]["id"]: item for item in book_updates(db)}
    assert status["devdocs_en_bash"]["status"] == "update"
    assert status["devdocs_en_bash"]["latest"]["uuid"] == entry["uuid"]
    assert status["gone_en_all"]["status"] == "unpublished"
    assert status["renamed"]["status"] == "current"  # matched by Name metadata, same uuid


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("hi", True),
        ("Hello!", True),
        ("hey there :)", True),
        ("Thanks a lot!", True),
        ("good morning", True),
        ("how are you?", True),
        ("ok", True),
        ("hi, how do I purify water?", False),
        ("hello world in rust", False),
        ("what is a kernel", False),
        ("thanks, and what about chlorine?", False),
    ],
)
def test_ai_small_talk(message, expected):
    from zim_pro.ai import is_small_talk

    assert is_small_talk(message) is expected


def test_ai_chat_prompt_has_no_sources_block():
    from zim_pro.ai import build_messages

    chat = build_messages("hi", [], None)
    assert chat[-1]["content"] == "hi" and "library" in chat[0]["content"]
    assert "found nothing relevant" in build_messages("x", [], [])[-1]["content"]


def test_ai_single_provider_is_migrated():
    from zim_pro import providers

    db = Database(":memory:")
    for key, value in (
        ("ai_base_url", "https://generativelanguage.googleapis.com/v1beta/openai/"),
        ("ai_api_key", "secret"),
        ("ai_model", "models/gemma"),
    ):
        db.execute("INSERT INTO settings (key, value) VALUES (?, json_quote(?))", (key, value))
    providers.migrate_single_provider(db)
    providers.migrate_single_provider(db)  # running again must not add a second one
    (provider,) = providers.list_providers(db)
    assert (provider.kind, provider.api_key) == ("gemini", "secret")
    assert provider.base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert provider.public()["has_key"] and "api_key" not in provider.public()
    settings = db.get_settings()
    assert settings["ai_provider"] == provider.id and settings["ai_model"] == "models/gemma"


def test_ai_citation_shapes():
    from zim_pro.providers import _citations, _wav

    perplexity = {"citations": ["https://a.example/1"], "search_results": []}
    assert _citations(perplexity, {}) == [{"url": "https://a.example/1", "title": ""}]
    results = {"search_results": [{"url": "https://a.example/1", "title": "A"}]}
    assert _citations(results, {}) == [{"url": "https://a.example/1", "title": "A"}]
    note = {"type": "url_citation", "url_citation": {"url": "https://b.example", "title": "B"}}
    assert _citations({}, {"annotations": [note]}) == [{"url": "https://b.example", "title": "B"}]
    assert _wav(b"\x00\x00" * 10)[:4] == b"RIFF" and len(_wav(b"\x00\x00" * 10)) == 44 + 20


def test_ai_web_fallback_decision_and_numbering():
    from zim_pro.ai import Source, library_is_enough, web_sources
    from zim_pro.api.ai import _number_citations

    weak = Source(1, "library", "Water", "…", coverage=0.2)
    strong = Source(2, "library", "Water purification", "…", coverage=0.8)
    assert not library_is_enough([]) and not library_is_enough([weak])
    assert library_is_enough([weak, strong])
    web = web_sources([{"url": "https://www.example.org/a", "title": "", "text": "x"}], 3)
    assert (web[0].n, web[0].kind, web[0].site, web[0].title) == (
        3,
        "web",
        "example.org",
        "example.org",
    )
    assert not library_is_enough(web)
    text = (
        "It worked ([example.org](https://www.example.org/a)), see [docs](https://other.example)."
    )
    assert _number_citations(text, ["https://www.example.org/a"]) == (
        "It worked [1], see [docs](https://other.example)."
    )


def test_ai_prompts_carry_formatting_rules_and_user_instructions():
    from zim_pro.ai import Source, build_messages, web_messages

    cited = build_messages("q", [], [Source(1, "library", "T", "x")], "Answer in Hindi.")[0][
        "content"
    ]
    assert "Formatting rules" in cited and "one number per bracket" in cited
    assert cited.rstrip().endswith("Answer in Hindi.")
    chat = build_messages("hi", [], None)[0]["content"]
    assert "Formatting rules" in chat and "one number per bracket" not in chat
    assert "asked for this in every answer" not in chat
    assert "Formatting rules" in web_messages("q", [])[0]["content"]


def _split_stream(chunks):
    from zim_pro.providers import ReasoningSplitter

    splitter, reasoning, answer, retracted = ReasoningSplitter(), "", "", 0
    for chunk in chunks:
        for kind, value in splitter.feed(chunk):
            if kind == "reasoning":
                reasoning += value
            elif kind == "text":
                answer += value
            else:
                reasoning, answer, retracted = reasoning + "", "", retracted + 1
    for kind, value in splitter.flush():
        reasoning, answer = (
            (reasoning + value, answer) if kind == "reasoning" else (reasoning, answer + value)
        )
    return reasoning, answer, retracted


@pytest.mark.parametrize("tag", ["think", "thought", "thinking", "reasoning"])
def test_reasoning_is_split_from_the_answer(tag):
    text = f"<{tag}>The user wants X.\nPlan: answer.</{tag}>\n\nThe answer is **42** [1]."
    expected = ("The user wants X.\nPlan: answer.", "The answer is **42** [1].", 0)
    assert _split_stream([text]) == expected
    # The same text arriving a few characters at a time, with tags cut in half.
    for size in (1, 3, 7):
        assert _split_stream([text[i : i + size] for i in range(0, len(text), size)]) == expected


def test_reasoning_edge_cases():
    from zim_pro.providers import split_reasoning

    assert _split_stream(["Plain answer with a < b and <b>bold</b>."]) == (
        "",
        "Plain answer with a < b and <b>bold</b>.",
        0,
    )
    # Only the closing tag (the opening one lives in the model's prompt template).
    assert _split_stream(["Let me think. ", "Done.</think>", "The answer."]) == (
        "Let me think. Done.",
        "The answer.",
        1,
    )
    # Stopped while still thinking.
    assert _split_stream(["<thought>half a thou"]) == ("half a thou", "", 0)
    # A tag mentioned later in an answer is not thinking.
    late = "Use the tag <think> to mark it.</think> Done."
    assert split_reasoning("Answer first. " + late) == ("", "Answer first. " + late)
    assert split_reasoning("<thought>a</thought>b") == ("a", "b")


def test_free_providers_are_seeded_once():
    from zim_pro import providers

    db = Database(":memory:")
    providers.seed_free_providers(db)
    seeded = providers.list_providers(db)
    assert sorted(p.id for p in seeded) == ["free-kilo", "free-ovh"]
    assert all(p.public()["free"] and not p.api_key for p in seeded)
    settings = db.get_settings()
    assert (settings["ai_provider"], settings["ai_model"]) == (
        "free-ovh",
        "Meta-Llama-3_3-70B-Instruct",
    )
    # Removing one is respected on the next start.
    providers.delete_provider(db, "free-kilo")
    providers.seed_free_providers(db)
    assert [p.id for p in providers.list_providers(db)] == ["free-ovh"]


def test_seeding_keeps_an_existing_default():
    from zim_pro import providers

    db = Database(":memory:")
    mine = providers.save_provider(
        db, providers.Provider("mine", "Mine", "gemini", "https://x/v1", "k")
    )
    db.set_settings({"ai_provider": mine.id, "ai_model": "models/gemma"})
    providers.seed_free_providers(db)
    assert db.get_setting("ai_provider") == "mine" and db.get_setting("ai_model") == "models/gemma"
    assert len(providers.list_providers(db)) == 3 and not mine.public()["free"]


def test_free_model_lists_are_filtered():
    from zim_pro.providers import Provider, usable_models

    kilo = Provider("k", "Kilo", "kilo", "https://api.kilo.ai/api/gateway/v1", "")
    listed = [
        "anthropic/claude",
        "nvidia/nemotron:free",
        "kilo-auto/free",
        "nvidia/content-safety:free",
    ]
    assert usable_models(kilo, listed) == ["nvidia/nemotron:free"]
    assert "anthropic/claude" in usable_models(Provider("k", "Kilo", "kilo", "u", "key"), listed)
    ovh = Provider("o", "OVH", "ovh", "u", "")
    mixed = [
        "Meta-Llama-3_3-70B-Instruct",
        "whisper-large-v3",
        "bge-m3",
        "Qwen3Guard-Gen-8B",
        "gpt-oss-120b",
    ]
    assert usable_models(ovh, mixed) == ["Meta-Llama-3_3-70B-Instruct", "gpt-oss-120b"]


BOOKS = [
    {"id": "wikipedia_en_all_maxi", "title": "Wikipedia", "description": "The free encyclopedia",
     "tags": "wikipedia", "article_count": 6_800_000, "path": "/w.zim", "category": "wikipedia"},
    {"id": "appropedia_en_all_maxi", "title": "Appropedia", "description": "Sustainability wiki",
     "tags": "", "article_count": 18_000, "path": "/a.zim"},
    {"id": "askubuntu.com_en_all", "title": "Ask Ubuntu Q&A", "description": "Q&A for Ubuntu users",
     "tags": "", "article_count": 780_000, "path": "/u.zim"},
    {"id": "devdocs_en_python", "title": "Python Docs", "description": "Python docs by DevDocs",
     "tags": "devdocs;python", "article_count": 500, "path": "/p.zim"},
    {"id": "devdocs_en_bash", "title": "Bash Docs", "description": "Bash docs by DevDocs",
     "tags": "devdocs;bash", "article_count": 130, "path": "/b.zim"},
]  # fmt: skip
BOOK_IDS = {b["id"] for b in BOOKS}


def test_catalog_folds_devdocs_into_one_line():
    from zim_pro.retrieval import catalog

    text = catalog(BOOKS)
    assert "wikipedia_en_all_maxi — Wikipedia: The free encyclopedia" in text
    assert text.count("devdocs_en_") == 1 and text.rstrip().endswith("bash, python")


def test_parse_plan_reads_queries_keywords_and_books():
    from zim_pro.retrieval import parse_plan

    reply = (
        "<thought>the user wants water safety</thought>"
        'QUERIES:\n- water purification\n2. "emergency drinking water"\nboiling water\n'
        "KEYWORDS: chlorination, iodine tablets, water, filtration\n"
        "BOOKS: appropedia_en_all_maxi, wikipedia_en_all_maxi, python, not_a_book"
    )
    plan = parse_plan(reply, BOOK_IDS)
    assert plan.queries == ["water purification", "emergency drinking water", "boiling water"]
    # "water" only repeats the queries, so it is dropped.
    assert plan.keywords == ["chlorination", "iodine tablets", "filtration"]
    assert plan.books == ["appropedia_en_all_maxi", "wikipedia_en_all_maxi", "devdocs_en_python"]


def test_parse_plan_tolerates_other_shapes():
    from zim_pro.retrieval import parse_plan

    assert not parse_plan("NONE", BOOK_IDS) and not parse_plan(" none. ", BOOK_IDS)
    # No headers at all: every line is a query.
    assert parse_plan("causes first world war\njuly crisis", BOOK_IDS).queries == [
        "causes first world war",
        "july crisis",
    ]
    inline = parse_plan(
        "**Queries:** kernel, operating system kernel\nBooks: wikipedia_en_all_maxi", BOOK_IDS
    )
    assert inline.queries == ["kernel", "operating system kernel"]
    assert inline.books == ["wikipedia_en_all_maxi"]


def test_plan_locally_and_history():
    from zim_pro.ai import clean_history
    from zim_pro.retrieval import plan_locally

    assert not plan_locally("hi", [])
    plan = plan_locally("How do I purify water in an emergency?", [])
    assert plan.queries == ["purify water emergency", "purify water"]
    history = [
        {"role": "user", "content": "How do I purify water?"},
        {"role": "assistant", "content": "Boil."},
    ]
    assert plan_locally("and with chlorine?", history).queries[0] == "purify water chlorine"
    cleaned = clean_history(
        [{"role": "assistant", "content": "<thought>secret plan</thought>Hello!"}]
    )
    assert cleaned == [{"role": "assistant", "content": "Hello!"}]


def test_books_are_routed_by_name_and_chosen():
    from zim_pro.retrieval import MAX_FOCUS_BOOKS, Plan, choose_focus, route_books

    assert route_books(["python", "reverse list"], BOOKS) == ["devdocs_en_python"]
    assert route_books(["ubuntu", "disk space"], BOOKS) == ["askubuntu.com_en_all"]
    assert route_books(["photosynthesis"], BOOKS) == []
    many = BOOKS + [
        {**BOOKS[4], "id": f"devdocs_en_x{i}", "title": f"X{i} Docs", "description": "", "tags": ""}
        for i in range(MAX_FOCUS_BOOKS)
    ]
    # The model's pick, the book named after the subject, and always the encyclopedia.
    picked = Plan(queries=["reverse list python"], books=["appropedia_en_all_maxi"])
    focus = choose_focus(picked, "how do I make a reversed list", many)
    assert [b["id"] for b in focus] == [
        "appropedia_en_all_maxi",
        "devdocs_en_python",
        "wikipedia_en_all_maxi",
    ]
    # Nothing chosen and nothing named after the subject: the encyclopedia still is.
    fallback = choose_focus(Plan(queries=["photosynthesis"]), "photosynthesis", many)
    assert [b["id"] for b in fallback] == ["wikipedia_en_all_maxi"]
    # A small scope is searched in depth as a whole.
    assert len(choose_focus(Plan(queries=["x"]), "x", BOOKS)) == len(BOOKS)


def test_passages_keep_intro_and_score_keywords():
    from zim_pro.retrieval import _passages

    intro = "Water purification is the process of removing contaminants from water for drinking."
    filler = "\n\n".join(
        f"Paragraph {i} discusses unrelated history of the region at length." for i in range(80)
    )
    target = "Boiling is the simplest method: a rolling boil for one minute kills most pathogens."
    coverage, extra, excerpt = _passages(
        f"{intro}\n\n{filler}\n\n{target}",
        {"water", "purification"},
        {"boiling", "pathogens", "chlorine"},
    )
    assert excerpt.startswith(intro) and target in excerpt and len(excerpt) < 3000
    assert coverage == 1.0 and extra == pytest.approx(2 / 3)


class _FakeSearch:
    """Stands in for the ZIM search: each (book, query) maps to a list of titles."""

    def __init__(self, table):
        self.table = table

    def __call__(self, archive, query, start, count, snippets=True):
        titles = self.table.get((archive, query), [])[:count]
        return {
            "total": len(titles),
            "mode": "fulltext",
            "results": [{"path": t, "title": t} for t in titles],
        }


def test_search_fuses_results_across_queries(monkeypatch):
    from types import SimpleNamespace

    from zim_pro import retrieval

    table = {
        ("/w.zim", "water purification"): ["Water purification", "Water", "Purification rite"],
        ("/w.zim", "boiling water"): ["Boiling", "Water purification"],
        ("/w.zim", "chlorination"): ["Water chlorination", "Water purification"],
        ("/a.zim", "safe water emergency"): [],
        ("/a.zim", "safe water"): ["Emergency water"],
    }
    monkeypatch.setattr(retrieval.zim_search, "search_book", _FakeSearch(table))
    ctx = SimpleNamespace(pool=SimpleNamespace(get=lambda path: path))
    found: retrieval.Candidates = {}
    queries = [("water purification", 1.0), ("boiling water", 1.0), ("chlorination", 0.6)]
    hits = retrieval.search_books(ctx, BOOKS[:1], queries, 8, found)
    assert hits == 7 and len(found) == 5
    ranked = sorted(found.values(), key=lambda c: -c["score"])
    # Found by all three searches, so it beats articles that topped only one.
    assert ranked[0]["title"] == "Water purification" and len(ranked[0]["found_by"]) == 3
    # A query that finds nothing is retried with fewer words when relaxing, at a discount.
    retrieval.search_books(ctx, BOOKS[1:2], [("safe water emergency", 1.0)], 8, found, relax=True)
    relaxed = found[("appropedia_en_all_maxi", "Emergency water")]
    assert relaxed["score"] == pytest.approx(0.6 / retrieval.RRF_K)
    # The only page of a one-page book "ranks first" for everything, which means little.
    tiny = {**BOOKS[1], "id": "one_page", "path": "/w.zim", "article_count": 1}
    retrieval.search_books(ctx, [tiny], [("water purification", 1.0)], 8, found)
    assert found[("one_page", "Water purification")]["score"] < 0.2 / retrieval.RRF_K


def test_long_pages_do_not_win_on_scattered_words():
    from zim_pro.retrieval import _passages

    terms = {"ubuntu", "disk", "space", "free"}
    focused = (
        "To free disk space on Ubuntu, remove old kernels and clear the apt cache with apt clean."
    )
    scattered = "\n\n".join(
        f"Section {i} is a long paragraph about {word} and many other unrelated things entirely."
        for i, word in enumerate(
            ["ubuntu", "php", "disk", "java", "space", "css", "free", "html"] * 12
        )
    )
    on_topic = _passages(f"Free up disk space.\n\n{focused}", terms)[0]
    long_page = _passages(scattered, terms)[0]
    assert on_topic == 1.0 and long_page == pytest.approx(0.6 * 0.25 + 0.4)


def test_keywords_are_tied_to_the_subject():
    from zim_pro.retrieval import KEYWORD_WEIGHT, Plan, search_list

    plan = Plan(
        queries=["water purification", "boiling water", "emergency water"],
        keywords=["chlorine", "solar still"],
    )
    assert search_list(plan) == [
        ("water purification", 1.0),
        ("boiling water", 1.0),
        ("emergency water", 1.0),
        ("chlorine water", KEYWORD_WEIGHT),
        ("solar still water", KEYWORD_WEIGHT),
    ]
    # No word runs through the queries: keywords are searched as they are.
    lone = search_list(Plan(queries=["kernel", "scheduler"], keywords=["mutex"]))
    assert lone[-1] == ("mutex", KEYWORD_WEIGHT)


WIKI_PAGE = b"""<html><body>
<div class="thumb"><img src="../I/still.png"><div class="thumbcaption">A still in a pit</div></div>
<p>A <b>solar still</b> <a href="Distillation">distills water</a> using the sun.
<sup class="reference"><a href="#cite_note-1">[1]</a></sup></p>
<h2>History</h2>
<p>Stills are old. See <a class="reference internal" href="#x"><code>pit()</code></a>.</p>
<h2>See also</h2><ul><li><a href="Desalination">Desalination</a></li></ul>
<h2>References</h2><ol class="references"><li>Some book, 1975.</li></ol>
<h2>External links</h2><ul><li><a href="https://example.org/still">Making one</a></li></ul>
<div>This article is issued from <a href="https://en.wikipedia.org">Wikipedia</a>. The text
is licensed under Creative Commons.</div>
</body></html>"""


def test_markdown_for_a_model_keeps_only_the_prose():
    from zim_pro.library.article import to_markdown
    from zim_pro.library.reader import Article

    text = to_markdown(Article("A/Solar_still", "Solar still", "text/html", WIKI_PAGE))
    assert text.startswith("# Solar still\n\nA **solar still** distills water using the sun.")
    assert "## History" in text
    # Text inside a Sphinx cross-reference link is kept; only citation markers go.
    assert "`pit()`" in text and "[1]" not in text
    for gone in ("A still in a pit", "See also", "References", "External links", "Some book"):
        assert gone not in text
    for gone in ("issued from", "]("):
        assert gone not in text


def test_markdown_page_links_to_other_markdown_pages():
    from zim_pro.library.article import to_markdown
    from zim_pro.library.reader import Article

    article = Article("A/Solar_still", "Solar still", "text/html", WIKI_PAGE)
    text = to_markdown(article, "wikipedia_en_all")
    assert "[distills water](/markdown/wikipedia_en_all/A/Distillation)" in text
    assert "- [Desalination](/markdown/wikipedia_en_all/A/Desalination)" in text
    assert "[Making one](https://example.org/still)" in text
    # The reference list is gone, and so is the heading it left empty.
    assert "References" not in text and "issued from" not in text


def test_passages_name_their_section():
    from zim_pro.retrieval import _passages

    intro = "A solar still purifies water with the heat of the sun and nothing else at all."
    body = "A pit still is dug in the ground and covered with a sheet of clear plastic film."
    text = f"# Solar still\n\n{intro}\n\n## Methods\n\n### Pit still\n\n{body}"
    excerpt = _passages(text, {"pit", "still"})[2]
    assert excerpt == f"{intro}\n\n### Pit still\n\n{body}"


MEDIA_PAGE = b"""<html><body>
<header><img src="logo.png" alt="Site"></header>
<div class="thumb"><a href="../I/still.png"><img src="../I/still.png" width="300" height="94"></a>
<div class="thumbcaption">A still in a pit .</div></div>
<p>Text. <img src="../I/flag.png" width="20" height="12" alt="flag"> More text.</p>
<p><img src="info-icon.png"><img src="https://example.org/far.png" width="400"></p>
<video poster="videos/1/thumb.webp" controls><source src="videos/1/video.webm"></video>
</body></html>"""


def test_media_lists_pictures_and_videos_but_not_decoration():
    from zim_pro.library.article import media
    from zim_pro.library.reader import Article

    found = media(Article("A/Solar_still", "Solar still", "text/html", MEDIA_PAGE), "wiki")
    assert found == [
        {"kind": "image", "url": "/content/wiki/I/still.png", "caption": "A still in a pit."},
        {
            "kind": "video",
            "poster": "/content/wiki/A/videos/1/thumb.webp",
            "url": "/content/wiki/A/videos/1/video.webm",
            "caption": "",
        },
    ]


def test_markdown_page_keeps_pictures_and_a_model_gets_none():
    from zim_pro.library.article import to_markdown
    from zim_pro.library.reader import Article

    article = Article("A/Solar_still", "Solar still", "text/html", MEDIA_PAGE)
    page = to_markdown(article, "wiki")
    assert "![A still in a pit.](/content/wiki/I/still.png)" in page
    assert "[Video: video.webm](/content/wiki/A/videos/1/video.webm)" in page
    # The caption is the picture's description, not a paragraph of its own as well.
    assert page.count("A still in a pit") == 1 and "flag" not in page and "logo" not in page
    for_model = to_markdown(article)
    assert "![" not in for_model and "still in a pit" not in for_model and "video" not in for_model
