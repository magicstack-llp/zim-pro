import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, readUrl, type Book } from "../lib/api";
import { Combobox } from "../Combobox";
import { ago, categoryLabel, count } from "../lib/format";
import { useData } from "../lib/live";
import { usePhone, useTouch } from "../lib/viewport";
import { bookOptions, ScopeBar } from "../Scope";
import { BookIcon, Empty, ErrorNote } from "../ui";
import { bookIcon } from "./Library";

interface Hit {
  path: string;
  title: string;
  snippet?: string;
}
interface Group {
  book: string;
  title: string;
  has_illustration: number;
  total: number;
  mode: "fulltext" | "title";
  results: Hit[];
}
interface Results {
  book?: string;
  total?: number;
  mode?: "fulltext" | "title";
  results?: Hit[];
  books?: Group[];
}

interface Past {
  id: number;
  ts: number;
  query: string;
  book: string;
  category: string;
}
interface History {
  enabled: boolean;
  items: Past[];
}

const PAGE = 20;
const HISTORY_OPEN = "searchHistoryOpen";
const HISTORY_ICON = "M3.5 10a6.5 6.5 0 1 0 2-4.7M3.5 4v3.5H7M10 6.5V10l2.5 1.5";

/** Past searches, in a panel on the right; choosing one runs it again. */
function HistoryPanel({
  history,
  books,
  onPick,
  onForget,
  onClear,
  onClose,
}: {
  history: History | null;
  books: Book[];
  onPick: (past: Past) => void;
  onForget: (past: Past) => void;
  onClear: () => void;
  onClose: () => void;
}) {
  const scope = (past: Past) => (past.book ? (books.find((b) => b.id === past.book)?.title ?? past.book) : past.category ? categoryLabel(past.category) : "");
  return (
    <>
      <button className="fixed inset-0 z-30 bg-black/40 lg:hidden" aria-label="Close search history" onClick={onClose} />
      <aside
        aria-label="Search history"
        className="fixed top-0 right-0 bottom-[calc(3.5rem+env(safe-area-inset-bottom))] z-30 flex w-80 max-w-[85vw] flex-col border-l border-zinc-200 bg-white shadow-xl md:bottom-0 lg:shadow-none dark:border-zinc-800 dark:bg-zinc-900"
      >
        <header className="flex shrink-0 items-center gap-2 border-b border-zinc-200 py-2 pr-2 pl-4 dark:border-zinc-800">
          <h2 className="flex-1 text-base font-semibold">Search history</h2>
          {!!history?.items.length && (
            <button className="muted rounded-lg px-2 py-1 text-xs hover:bg-zinc-100 hover:text-red-600 dark:hover:bg-zinc-800" onClick={onClear}>
              Clear all
            </button>
          )}
          <button className="flex h-9 w-9 items-center justify-center rounded-full hover:bg-zinc-100 dark:hover:bg-zinc-800" onClick={onClose} aria-label="Close">
            <svg viewBox="0 0 20 20" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden>
              <path d="m5.5 5.5 9 9M14.5 5.5l-9 9" />
            </svg>
          </button>
        </header>
        {history && !history.enabled && (
          <p className="border-b border-zinc-200 px-4 py-3 text-xs text-amber-700 dark:border-zinc-800 dark:text-amber-400">
            History is turned off, so new searches are not saved.{" "}
            <Link to="/settings" className="underline">
              Turn it on in Settings
            </Link>
            .
          </p>
        )}
        {history && history.items.length === 0 ? (
          <p className="muted px-4 py-8 text-center text-sm">{history.enabled ? "Your searches will appear here." : "Nothing saved."}</p>
        ) : (
          <ul className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-2">
            {history?.items.map((past) => (
              <li key={past.id} className="group flex items-center rounded-xl hover:bg-zinc-100 dark:hover:bg-zinc-800">
                <button className="min-w-0 flex-1 px-3 py-2 text-left" onClick={() => onPick(past)}>
                  <span className="block truncate text-sm font-medium">{past.query}</span>
                  <span className="muted block truncate text-xs">{[scope(past), ago(past.ts)].filter(Boolean).join(" · ")}</span>
                </button>
                <button
                  className="muted mr-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-full hover:bg-zinc-200 hover:text-red-600 sm:opacity-0 sm:group-hover:opacity-100 sm:focus:opacity-100 dark:hover:bg-zinc-700"
                  onClick={() => onForget(past)}
                  aria-label={`Remove “${past.query}” from history`}
                  title="Remove"
                >
                  <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden>
                    <path d="m6 6 8 8M14 6l-8 8" />
                  </svg>
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>
    </>
  );
}

export default function Search() {
  const touch = useTouch();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const book = params.get("book") ?? "";
  const category = params.get("category") ?? "";
  const start = Number(params.get("start") ?? 0);
  const [text, setText] = useState(q);
  useEffect(() => setText(q), [q]);

  const phone = usePhone();
  const [history, setHistory] = useState<History | null>(null);
  const [historyOpen, setHistoryOpen] = useState(() => {
    try {
      return localStorage.getItem(HISTORY_OPEN) === "1";
    } catch {
      return false;
    }
  });
  const showHistory = (open: boolean) => {
    setHistoryOpen(open);
    try {
      localStorage.setItem(HISTORY_OPEN, open ? "1" : "0");
    } catch {
      /* not remembered */
    }
  };
  const loadHistory = () =>
    api<History>("/api/search/history")
      .then(setHistory)
      .catch(() => undefined);
  useEffect(() => {
    if (historyOpen) loadHistory();
  }, [historyOpen]);
  // Each new search is remembered once, not once per page of its results.
  useEffect(() => {
    if (!q) return;
    api("/api/search/history", {
      method: "POST",
      body: JSON.stringify({ query: q, book, category }),
    })
      .then(loadHistory)
      .catch(() => undefined);
  }, [q, book, category]);

  const books = useData<Book[]>("/api/books");
  const query = new URLSearchParams({
    q,
    book,
    category,
    start: String(start),
    count: String(PAGE),
  });
  const { data, error, loading } = useData<Results>(q ? `/api/search?${query}` : null);

  const choices = useMemo(() => (books.data ?? []).filter((b) => !category || b.category === category), [books.data, category]);
  const current = books.data?.find((b) => b.id === book);

  // Every change resets paging; `start` only survives when it is passed explicitly.
  const update = (next: Record<string, string>) => {
    const merged = { q, book, category, ...next };
    setParams(Object.fromEntries(Object.entries(merged).filter(([, value]) => value)));
  };
  const pickerOptions = useMemo(
    () => [
      {
        value: "",
        label: category ? `All ${categoryLabel(category)} books` : "All books",
      },
      ...bookOptions(choices, !category),
    ],
    [choices, category],
  );
  const scopeBar = <ScopeBar books={books.data ?? []} category={category} book={book} onChange={(scope) => update(scope)} center />;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    update({ q: text.trim() });
  };

  const historyPanel = historyOpen && (
    <HistoryPanel
      history={history}
      books={books.data ?? []}
      onPick={(past) => {
        update({ q: past.query, book: past.book, category: past.category });
        if (phone) showHistory(false);
      }}
      onForget={(past) => api(`/api/search/history/${past.id}`, { method: "DELETE" }).then(loadHistory)}
      onClear={() => api("/api/search/history", { method: "DELETE" }).then(loadHistory)}
      onClose={() => showHistory(false)}
    />
  );
  const form = (large: boolean) => (
    <div className="flex w-full items-center gap-2">
      <form onSubmit={submit} role="search" className="min-w-0 flex-1">
        <div
          className={`focus-within:border-brand-500 focus-within:ring-brand-500/30 flex items-center rounded-full border border-zinc-300 bg-white shadow-sm transition focus-within:ring-2 hover:shadow-md dark:border-zinc-700 dark:bg-zinc-900 ${
            large ? "px-5 py-1" : "px-4"
          }`}
        >
          <svg viewBox="0 0 20 20" className="muted h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
            <circle cx="8.5" cy="8.5" r="5.5" />
            <path d="m13 13 4.5 4.5" strokeLinecap="round" />
          </svg>
          <input
            type="search"
            className={`min-w-0 flex-1 bg-transparent px-3 outline-none ${large ? "py-2.5 text-base" : "py-2 text-sm"}`}
            placeholder={current ? `Search ${current.title}` : "Search your library"}
            aria-label="Search"
            value={text}
            onChange={(e) => setText(e.target.value)}
            autoFocus={!touch}
            enterKeyHint="search"
          />
          <div className="shrink-0 border-l border-zinc-200 pl-3 dark:border-zinc-700">
            <Combobox
              value={book}
              options={pickerOptions}
              placeholder="All books"
              searchPlaceholder="Search books…"
              align="right"
              className="muted flex max-w-28 items-center gap-1.5 py-1 text-sm sm:max-w-48"
              onChange={(value) => update({ book: value })}
            />
          </div>
        </div>
      </form>
      <button
        type="button"
        className={`flex shrink-0 items-center justify-center rounded-full border transition ${large ? "h-12 w-12" : "h-10 w-10"} ${
          historyOpen
            ? "border-brand-500 bg-brand-50 text-brand-700 dark:bg-brand-900/50 dark:text-brand-100"
            : "border-zinc-300 bg-white text-zinc-600 hover:bg-zinc-100 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-300 dark:hover:bg-zinc-800"
        }`}
        onClick={() => showHistory(!historyOpen)}
        aria-pressed={historyOpen}
        aria-label="Search history"
        title="Search history"
      >
        <svg viewBox="0 0 20 20" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d={HISTORY_ICON} />
        </svg>
      </button>
    </div>
  );

  // On a wide screen the panel sits beside the page instead of over it.
  const beside = historyOpen ? "lg:mr-80" : "";
  if (!q) {
    return (
      <div className={beside}>
        {historyPanel}
        <div className="mx-auto flex min-h-[calc(100dvh-12rem)] max-w-2xl flex-col items-center justify-center gap-6 pb-10 kb:min-h-0 kb:justify-start kb:pt-2 sm:min-h-[70vh] sm:pb-24">
          <h1 className="flex items-center gap-3 text-4xl font-semibold tracking-tight sm:text-5xl">
            <img src="/icon-192.png" alt="" className="h-12 w-12 rounded-2xl ring-1 ring-zinc-200 sm:h-14 sm:w-14 dark:ring-0" />
            Zim Pro
          </h1>
          {form(true)}
          {scopeBar}
          {books.data && (
            <p className="muted text-sm">
              {count(choices.length)} {choices.length === 1 ? "book" : "books"} · {count(choices.reduce((sum, b) => sum + b.article_count, 0))} articles, searchable offline
            </p>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className={beside}>
      {historyPanel}
      <div className="mx-auto max-w-3xl">
        <div className="sticky top-0 z-10 -mx-4 bg-zinc-50 px-4 pt-1 pb-3 dark:bg-zinc-950">
          {form(false)}
          <div className="mt-3">{scopeBar}</div>
        </div>
        <ErrorNote message={error} />
        {loading && <p className="muted py-6 text-center text-sm">Searching…</p>}

        {!loading && data?.books && (
          <>
            {data.books.length === 0 ? (
              <Empty title={`Nothing found for “${q}”`}>{category && `No ${categoryLabel(category)} book has a match. Try all categories.`}</Empty>
            ) : (
              <p className="muted mb-3 text-sm">
                Matches in {count(data.books.length)} {data.books.length === 1 ? "book" : "books"}
              </p>
            )}
            <div className="space-y-3">
              {data.books.map((group) => (
                <section key={group.book} className="card p-4">
                  <div className="mb-2 flex items-center gap-2.5">
                    <BookIcon
                      src={bookIcon({
                        id: group.book,
                        has_illustration: group.has_illustration,
                      })}
                      title={group.title}
                      size={28}
                    />
                    <h2 className="min-w-0 flex-1 truncate font-medium">
                      {group.title} <span className="muted text-xs font-normal">{group.book}</span>
                    </h2>
                    <button className="text-brand-600 py-1 text-sm whitespace-nowrap hover:underline" onClick={() => update({ book: group.book })}>
                      {group.mode === "fulltext" ? `About ${count(group.total)} results` : `${count(group.total)} title matches`} →
                    </button>
                  </div>
                  <ul className="space-y-0.5">
                    {group.results.map((hit) => (
                      <li key={hit.path}>
                        <Link className="hover:text-brand-600 block truncate py-1 text-[15px] hover:underline sm:py-0.5 sm:text-sm" to={readUrl(group.book, hit.path)}>
                          {hit.title}
                        </Link>
                      </li>
                    ))}
                  </ul>
                </section>
              ))}
            </div>
          </>
        )}

        {!loading && data?.results && (
          <>
            <p className="muted mb-4 text-sm">
              {data.total
                ? `About ${count(data.total)} ${data.mode === "title" ? "title matches (this book has no full-text index)" : "results"} in ${current?.title ?? book}`
                : `Nothing found for “${q}” in ${current?.title ?? book}`}
            </p>
            <ul className="space-y-6">
              {data.results.map((hit) => (
                <li key={hit.path}>
                  <div className="muted truncate text-xs">
                    {current?.title} › {hit.path}
                  </div>
                  <Link className="text-brand-700 dark:text-brand-400 text-lg hover:underline" to={readUrl(book, hit.path)}>
                    {hit.title}
                  </Link>
                  {hit.snippet && <p className="mt-0.5 line-clamp-3 text-sm text-zinc-600 dark:text-zinc-300">{hit.snippet}</p>}
                </li>
              ))}
            </ul>
            {(start > 0 || data.results.length >= PAGE) && (
              <div className="mt-8 flex justify-center gap-3">
                <button className="btn" disabled={start === 0} onClick={() => update({ start: String(Math.max(0, start - PAGE)) })}>
                  ← Previous
                </button>
                <button className="btn" disabled={data.results.length < PAGE || start + PAGE >= (data.total ?? 0)} onClick={() => update({ start: String(start + PAGE) })}>
                  Next →
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
