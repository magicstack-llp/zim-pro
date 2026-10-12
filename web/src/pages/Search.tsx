import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { readUrl, type Book } from "../lib/api";
import { Combobox } from "../Combobox";
import { categoryLabel, count } from "../lib/format";
import { useData } from "../lib/live";
import { useTouch } from "../lib/viewport";
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

const PAGE = 20;

export default function Search() {
  const touch = useTouch();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const book = params.get("book") ?? "";
  const category = params.get("category") ?? "";
  const start = Number(params.get("start") ?? 0);
  const [text, setText] = useState(q);
  useEffect(() => setText(q), [q]);

  const books = useData<Book[]>("/api/books");
  const query = new URLSearchParams({ q, book, category, start: String(start), count: String(PAGE) });
  const { data, error, loading } = useData<Results>(q ? `/api/search?${query}` : null);

  const choices = useMemo(
    () => (books.data ?? []).filter((b) => !category || b.category === category),
    [books.data, category],
  );
  const current = books.data?.find((b) => b.id === book);

  // Every change resets paging; `start` only survives when it is passed explicitly.
  const update = (next: Record<string, string>) => {
    const merged = { q, book, category, ...next };
    setParams(Object.fromEntries(Object.entries(merged).filter(([, value]) => value)));
  };
  const pickerOptions = useMemo(
    () => [
      { value: "", label: category ? `All ${categoryLabel(category)} books` : "All books" },
      ...bookOptions(choices, !category),
    ],
    [choices, category],
  );
  const scopeBar = (
    <ScopeBar books={books.data ?? []} category={category} book={book} onChange={(scope) => update(scope)} center />
  );
  const submit = (event: FormEvent) => {
    event.preventDefault();
    update({ q: text.trim() });
  };

  const form = (large: boolean) => (
    <form onSubmit={submit} role="search" className="w-full">
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
  );

  if (!q) {
    return (
      <div className="mx-auto flex min-h-[calc(100dvh-12rem)] max-w-2xl flex-col items-center justify-center gap-6 pb-10 kb:min-h-0 kb:justify-start kb:pt-2 sm:min-h-[70vh] sm:pb-24">
        <h1 className="flex items-center gap-3 text-4xl font-semibold tracking-tight sm:text-5xl">
          <span className="bg-brand-700 flex h-12 w-12 items-center justify-center rounded-2xl text-2xl font-bold text-white sm:h-14 sm:w-14">
            Z
          </span>
          Zim Pro
        </h1>
        {form(true)}
        {scopeBar}
        {books.data && (
          <p className="muted text-sm">
            {count(choices.length)} {choices.length === 1 ? "book" : "books"} ·{" "}
            {count(choices.reduce((sum, b) => sum + b.article_count, 0))} articles, searchable offline
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl">
      <div className="sticky top-0 z-10 -mx-4 bg-zinc-50 px-4 pt-1 pb-3 dark:bg-zinc-950">
        {form(false)}
        <div className="mt-3">
          {scopeBar}
        </div>
      </div>
      <ErrorNote message={error} />
      {loading && <p className="muted py-6 text-center text-sm">Searching…</p>}

      {!loading && data?.books && (
        <>
          {data.books.length === 0 ? (
            <Empty title={`Nothing found for “${q}”`}>
              {category && `No ${categoryLabel(category)} book has a match. Try all categories.`}
            </Empty>
          ) : (
            <p className="muted mb-3 text-sm">
              Matches in {count(data.books.length)} {data.books.length === 1 ? "book" : "books"}
            </p>
          )}
          <div className="space-y-3">
            {data.books.map((group) => (
              <section key={group.book} className="card p-4">
                <div className="mb-2 flex items-center gap-2.5">
                  <BookIcon src={bookIcon({ id: group.book, has_illustration: group.has_illustration })} title={group.title} size={28} />
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
              <button
                className="btn"
                disabled={data.results.length < PAGE || start + PAGE >= (data.total ?? 0)}
                onClick={() => update({ start: String(start + PAGE) })}
              >
                Next →
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
