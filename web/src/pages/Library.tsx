import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { readUrl, type Book, type Status } from "../lib/api";
import { bytes, count, language } from "../lib/format";
import { useData } from "../lib/live";
import { ScopeBar } from "../Scope";
import { BookIcon, Empty, ErrorNote, PageHeader } from "../ui";

export const bookIcon = (book: Pick<Book, "id" | "has_illustration">) =>
  book.has_illustration ? `/api/books/${encodeURIComponent(book.id)}/illustration` : null;

export default function Library() {
  const books = useData<Book[]>("/api/books");
  const status = useData<Status>("/api/status");
  const [filter, setFilter] = useState("");
  const [lang, setLang] = useState("");
  const [scope, setScope] = useState({ category: "", book: "" });
  const { category, book: only } = scope;
  const [sort, setSort] = useState("title");

  const languages = useMemo(
    () => [...new Set((books.data ?? []).flatMap((b) => b.language.split(",")).filter(Boolean))].sort(),
    [books.data],
  );
  const shown = useMemo(() => {
    const words = filter.toLowerCase().split(/\s+/).filter(Boolean);
    const list = (books.data ?? []).filter((book) => {
      const haystack = `${book.title} ${book.description} ${book.id} ${book.tags}`.toLowerCase();
      return (
        words.every((w) => haystack.includes(w)) &&
        (!lang || book.language.split(",").includes(lang)) &&
        (!category || book.category === category) &&
        (!only || book.id === only)
      );
    });
    const orders: Record<string, (a: Book, b: Book) => number> = {
      "title-desc": (a, b) => b.title.localeCompare(a.title),
      size: (a, b) => b.size - a.size,
      "size-asc": (a, b) => a.size - b.size,
      date: (a, b) => b.date.localeCompare(a.date),
      "date-asc": (a, b) => a.date.localeCompare(b.date),
      articles: (a, b) => b.article_count - a.article_count,
    };
    if (orders[sort]) list.sort(orders[sort]);
    return list;
  }, [books.data, filter, lang, category, only, sort]);

  return (
    <>
      <PageHeader
        title="Library"
        subtitle={
          status.data &&
          `${count(status.data.books)} books · ${bytes(status.data.library_bytes)} · ${bytes(status.data.disk_free)} free`
        }
      >
        <input
          className="input w-56"
          placeholder="Filter books…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          autoFocus
        />
        <select className="input w-auto" value={lang} onChange={(e) => setLang(e.target.value)}>
          <option value="">All languages</option>
          {languages.map((code) => (
            <option key={code} value={code}>
              {language(code)}
            </option>
          ))}
        </select>
        <select className="input w-auto" value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort by">
          <option value="title">Title A–Z</option>
          <option value="title-desc">Title Z–A</option>
          <option value="date">Newest first</option>
          <option value="date-asc">Oldest first</option>
          <option value="size">Largest first</option>
          <option value="size-asc">Smallest first</option>
          <option value="articles">Most articles</option>
        </select>
      </PageHeader>
      <ErrorNote message={books.error} />
      <div className="mb-4">
        <ScopeBar books={books.data ?? []} category={category} book={only} onChange={setScope} />
      </div>
      {books.data && shown.length === 0 && (
        <Empty title={books.data.length ? "No books match the filter" : "The library is empty"}>
          {!books.data.length && (
            <>
              Add books from the <Link className="text-brand-600 underline" to="/catalog">catalog</Link>, or copy
              .zim files into the data folder.
            </>
          )}
        </Empty>
      )}
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {shown.map((book) => (
          <Link
            key={book.id}
            to={readUrl(book.id)}
            className="card hover:border-brand-500 flex gap-3 p-3.5 transition hover:shadow-sm"
          >
            <BookIcon src={bookIcon(book)} title={book.title} size={44} />
            <div className="min-w-0 flex-1">
              <div className="truncate font-medium">{book.title}</div>
              <div className="muted line-clamp-2 min-h-8 text-xs">{book.description || book.id}</div>
              <div className="muted mt-1.5 flex flex-wrap gap-x-3 text-xs">
                <span>{bytes(book.size)}</span>
                <span>{count(book.article_count)} articles</span>
                <span>{book.date.slice(0, 7)}</span>
                {book.flavour && <span>{book.flavour}</span>}
              </div>
              {book.error && <div className="mt-1 text-xs text-red-600">Unreadable: {book.error}</div>}
            </div>
          </Link>
        ))}
      </div>
    </>
  );
}
