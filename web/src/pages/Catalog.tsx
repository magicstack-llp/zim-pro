import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { post, readUrl, type CatalogEntry, type Status } from "../lib/api";
import { ago, bytes, count, language } from "../lib/format";
import { useData, useLive } from "../lib/live";
import { BookIcon, Empty, ErrorNote, PageHeader } from "../ui";

interface Facets {
  languages: { value: string; count: number }[];
  categories: { value: string; count: number }[];
}
const PAGE = 48;

export default function Catalog() {
  const { refresh, downloads } = useLive();
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  const [lang, setLang] = useState("eng");
  const [category, setCategory] = useState("");
  const [sort, setSort] = useState("title");
  const [page, setPage] = useState(0);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const timer = setTimeout(() => {
      setQ(text.trim());
      setPage(0);
    }, 250);
    return () => clearTimeout(timer);
  }, [text]);

  const status = useData<Status>("/api/status");
  const facets = useData<Facets>("/api/catalog/facets");
  const query = new URLSearchParams({ q, language: lang, category, sort, start: String(page * PAGE), count: String(PAGE) });
  const { data, error, reload } = useData<{ total: number; results: CatalogEntry[] }>(`/api/catalog?${query}`, [
    downloads.length,
  ]);

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setProblem(null);
    try {
      await action();
      refresh();
      reload();
    } catch (reason) {
      setProblem(String((reason as Error).message));
    } finally {
      setBusy(false);
    }
  };
  const pages = data ? Math.ceil(data.total / PAGE) : 0;

  return (
    <>
      <PageHeader
        title="Catalog"
        subtitle={
          status.data &&
          `${count(status.data.catalog_entries)} books published by Kiwix · checked ${ago(status.data.catalog_refreshed)}`
        }
      >
        <button className="btn" disabled={busy} onClick={() => run(() => post("/api/catalog/refresh"))}>
          Refresh catalog
        </button>
      </PageHeader>
      <div className="mb-4 flex flex-wrap gap-2">
        <input className="input min-w-48 flex-1" placeholder="Search the catalog…" value={text} onChange={(e) => setText(e.target.value)} />
        <select className="input w-auto" value={lang} onChange={(e) => (setLang(e.target.value), setPage(0))}>
          <option value="">All languages</option>
          {facets.data?.languages.map((item) => (
            <option key={item.value} value={item.value}>
              {language(item.value)} ({item.count})
            </option>
          ))}
        </select>
        <select className="input w-auto" value={category} onChange={(e) => (setCategory(e.target.value), setPage(0))}>
          <option value="">All categories</option>
          {facets.data?.categories.map((item) => (
            <option key={item.value} value={item.value}>
              {item.value} ({item.count})
            </option>
          ))}
        </select>
        <select className="input w-auto" value={sort} onChange={(e) => (setSort(e.target.value), setPage(0))} aria-label="Sort by">
          <option value="title">Title A–Z</option>
          <option value="title_desc">Title Z–A</option>
          <option value="newest">Newest first</option>
          <option value="oldest">Oldest first</option>
          <option value="size">Largest first</option>
          <option value="smallest">Smallest first</option>
          <option value="articles">Most articles</option>
        </select>
      </div>
      <ErrorNote message={error ?? problem ?? status.data?.catalog_error ?? null} />
      {data && data.total === 0 && (
        <Empty title={status.data?.catalog_entries ? "No books match" : "The catalog has not been fetched yet"}>
          {!status.data?.catalog_entries && "Press “Refresh catalog” to download the list from library.kiwix.org."}
        </Empty>
      )}
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {data?.results.map((entry) => (
          <div key={entry.uuid} className="card flex flex-col p-3.5">
            <div className="flex gap-3">
              <BookIcon src={entry.illustration} title={entry.title} size={44} />
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium" title={entry.title}>
                  {entry.title}
                </div>
                <div className="muted line-clamp-2 min-h-8 text-xs">{entry.summary}</div>
              </div>
            </div>
            <div className="muted mt-2 flex flex-wrap gap-x-3 text-xs">
              <span>{bytes(entry.size)}</span>
              <span>{count(entry.article_count)} articles</span>
              <span>{entry.issued.slice(0, 10)}</span>
              <span>{language(entry.language)}</span>
              {entry.flavour && <span>{entry.flavour}</span>}
            </div>
            <div className="mt-3 flex items-center justify-between gap-2">
              <span className="muted truncate text-xs" title={entry.stem}>
                {entry.stem}
              </span>
              {entry.download_state ? (
                <Link to="/downloads" className="badge capitalize">
                  {entry.download_state}
                </Link>
              ) : entry.local === "current" ? (
                <Link to={readUrl(entry.book_id!)} className="btn">
                  Open
                </Link>
              ) : (
                <button
                  className={`btn ${entry.local ? "" : "btn-primary"}`}
                  disabled={busy}
                  onClick={() => run(() => post("/api/downloads", { catalog_uuid: entry.uuid }))}
                >
                  {entry.local === "outdated" ? "Update" : "Download"}
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
      {pages > 1 && (
        <div className="mt-5 flex items-center justify-between">
          <button className="btn" disabled={page === 0} onClick={() => setPage(page - 1)}>
            ← Previous
          </button>
          <span className="muted text-sm">
            Page {page + 1} of {pages}
          </span>
          <button className="btn" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>
            Next →
          </button>
        </div>
      )}
    </>
  );
}
