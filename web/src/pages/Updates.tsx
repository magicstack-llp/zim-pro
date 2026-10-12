import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, post, readUrl, type Book, type UpdateItem } from "../lib/api";
import { ago, bytes, day } from "../lib/format";
import { useData, useLive } from "../lib/live";
import { BookIcon, Empty, ErrorNote, PageHeader, Progress, Tabs } from "../ui";
import { bookIcon } from "./Library";

interface UpdatesData {
  items: UpdateItem[];
  update_count: number;
  update_bytes: number;
  disk_free: number;
  catalog_refreshed: number | null;
}
interface HistoryRow {
  id: number;
  ts: number;
  title: string;
  from_date: string;
  to_date: string;
  size: number;
  result: "updated" | "added" | "failed";
  message: string;
}
type Tab = "update" | "current" | "unpublished" | "history";

const POLICY_LABELS: Record<Book["policy"], string> = {
  auto: "Automatic",
  notify: "Ask first",
  pinned: "Pinned",
};

export default function Updates() {
  const { downloads, refresh } = useLive();
  // Refetch when a download finishes, so the row moves to "Up to date".
  const finished = downloads.filter((d) => d.state === "done").length;
  const { data, error, reload } = useData<UpdatesData>("/api/updates", [finished]);
  const [tab, setTab] = useState<Tab>("update");
  const history = useData<HistoryRow[]>(tab === "history" ? "/api/history" : null, [finished]);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState("title");

  const live = useMemo(() => new Map(downloads.map((d) => [d.id, d])), [downloads]);
  const groups = useMemo(() => {
    const by: Record<string, UpdateItem[]> = { update: [], current: [], unpublished: [] };
    const words = filter.toLowerCase().split(/\s+/).filter(Boolean);
    for (const item of data?.items ?? []) {
      const { book } = item;
      const haystack = `${book.title} ${book.id} ${book.description} ${book.tags}`.toLowerCase();
      if (words.every((word) => haystack.includes(word))) by[item.status].push(item);
    }
    // The size that matters is the download when there is one, the installed file otherwise.
    const size = (item: UpdateItem) => item.latest?.size ?? item.book.size;
    const orders: Record<string, (a: UpdateItem, b: UpdateItem) => number> = {
      "title-desc": (a, b) => b.book.title.localeCompare(a.book.title),
      size: (a, b) => size(b) - size(a),
      "size-asc": (a, b) => size(a) - size(b),
      release: (a, b) => (b.latest?.issued ?? b.book.date).localeCompare(a.latest?.issued ?? a.book.date),
      installed: (a, b) => a.book.date.localeCompare(b.book.date),
    };
    if (orders[sort]) for (const list of Object.values(by)) list.sort(orders[sort]);
    return by;
  }, [data, filter, sort]);

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
  const setPolicy = (book: Book, policy: string) =>
    run(() => api(`/api/books/${encodeURIComponent(book.id)}`, { method: "PATCH", body: JSON.stringify({ policy }) }));

  const policySelect = (book: Book, width = "w-auto") => (
    <select
      className={`input ${width} px-2 py-1`}
      value={book.policy}
      disabled={busy}
      onChange={(e) => setPolicy(book, e.target.value)}
      aria-label={`Update policy for ${book.title}`}
    >
      {Object.entries(POLICY_LABELS).map(([value, label]) => (
        <option key={value} value={value}>
          {label}
        </option>
      ))}
    </select>
  );

  const pendingBytes = groups.update
    .filter((item) => item.book.policy !== "pinned" && !item.download)
    .reduce((sum, item) => sum + (item.latest?.size ?? 0), 0);
  const tooBig = !!data && pendingBytes > data.disk_free;

  return (
    <>
      <PageHeader
        title="Updates"
        subtitle={data && `Catalog checked ${ago(data.catalog_refreshed)} · ${bytes(data.disk_free)} free on disk`}
      >
        <input
          type="search"
          className="input w-full sm:w-56"
          placeholder="Filter books…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          aria-label="Filter books"
        />
        <select className="input w-auto" value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort by">
          <option value="title">Title A–Z</option>
          <option value="title-desc">Title Z–A</option>
          <option value="release">Newest release first</option>
          <option value="installed">Most outdated first</option>
          <option value="size">Largest first</option>
          <option value="size-asc">Smallest first</option>
        </select>
        <button className="btn" disabled={busy} onClick={() => run(() => post("/api/catalog/refresh"))}>
          Check now
        </button>
        <button
          className="btn btn-primary"
          disabled={busy || pendingBytes === 0}
          onClick={() =>
            run(async () => {
              if (!filter.trim()) return post("/api/updates/start-all");
              for (const item of groups.update) {
                if (item.book.policy !== "pinned" && !item.download)
                  await post(`/api/updates/${encodeURIComponent(item.book.id)}/start`);
              }
            })
          }
        >
          {filter.trim() ? "Update shown" : "Update all"}
          {pendingBytes > 0 && ` (${bytes(pendingBytes)})`}
        </button>
      </PageHeader>
      <ErrorNote message={error ?? problem} />
      {tooBig && (
        <ErrorNote
          message={`All updates together need ${bytes(pendingBytes)} but only ${bytes(data!.disk_free)} is free. Old versions are removed as each update finishes, so this may still fit; downloads that do not fit will stop with an error.`}
        />
      )}
      <Tabs<Tab>
        value={tab}
        onChange={setTab}
        options={[
          { value: "update", label: "Available", count: groups.update.length },
          { value: "current", label: "Up to date", count: groups.current.length },
          { value: "unpublished", label: "No longer published", count: groups.unpublished.length },
          { value: "history", label: "History" },
        ]}
      />

      {tab === "history" ? (
        history.data?.length ? (
          <div className="card overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-zinc-200 dark:border-zinc-800">
                  <th className="th">When</th>
                  <th className="th">Book</th>
                  <th className="th">Version</th>
                  <th className="th">Size</th>
                  <th className="th">Result</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
                {history.data.map((row) => (
                  <tr key={row.id}>
                    <td className="td muted whitespace-nowrap">{day(row.ts)}</td>
                    <td className="td font-medium">{row.title}</td>
                    <td className="td whitespace-nowrap">
                      {row.from_date ? `${row.from_date} → ${row.to_date}` : row.to_date}
                    </td>
                    <td className="td whitespace-nowrap">{bytes(row.size)}</td>
                    <td className="td">
                      {row.result === "failed" ? (
                        <span className="text-red-600 dark:text-red-400">Failed: {row.message}</span>
                      ) : row.result === "added" ? (
                        "Added"
                      ) : (
                        "Updated"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title="No updates yet">Finished and failed updates are listed here.</Empty>
        )
      ) : !data ? null : groups[tab].length === 0 ? (
        <Empty
          title={
            filter.trim()
              ? `No books match “${filter.trim()}”`
              : tab === "update"
              ? data.catalog_refreshed
                ? "Everything is up to date"
                : "The catalog has not been fetched yet"
              : "Nothing here"
          }
        >
          {!filter.trim() && tab === "update" && !data.catalog_refreshed && "Press “Check now” to fetch the Kiwix catalog."}
        </Empty>
      ) : (
        <div className="card overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-zinc-200 dark:border-zinc-800">
                <th className="th">Book</th>
                <th className="th hidden lg:table-cell">{tab === "update" ? "Version" : "Installed"}</th>
                <th className="th hidden lg:table-cell">Size</th>
                <th className={`th ${tab === "update" ? "hidden sm:table-cell" : ""}`}>Policy</th>
                {tab === "update" && <th className="th" />}
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
              {groups[tab].map(({ book, latest, download }) => {
                const active = download && (live.get(download.id) ?? download);
                return (
                  <tr key={book.id}>
                    <td className="td w-full max-w-0">
                      <Link to={readUrl(book.id)} className="flex items-center gap-2.5 hover:underline">
                        <BookIcon src={bookIcon(book)} title={book.title} size={28} />
                        <span className="min-w-0">
                          <span className="block truncate font-medium">{book.title}</span>
                          <span className="muted hidden truncate text-xs lg:block">{book.id}</span>
                          <span className="muted block truncate text-xs lg:hidden">
                            {book.date.slice(0, 7)}
                            {latest && ` → ${latest.issued.slice(0, 7)}`} · {bytes(latest?.size ?? book.size)}
                          </span>
                        </span>
                      </Link>
                    </td>
                    <td className="td hidden whitespace-nowrap lg:table-cell">
                      {book.date.slice(0, 10)}
                      {latest && <span className="text-brand-600 dark:text-brand-400"> → {latest.issued.slice(0, 10)}</span>}
                    </td>
                    <td className="td hidden whitespace-nowrap lg:table-cell">
                      {latest ? (
                        <>
                          {bytes(latest.size)}
                          <span className="muted block text-xs">now {bytes(book.size)}</span>
                        </>
                      ) : (
                        bytes(book.size)
                      )}
                    </td>
                    <td className={`td ${tab === "update" ? "hidden sm:table-cell" : ""}`}>{policySelect(book)}</td>
                    {tab === "update" && (
                      <td className="td text-right whitespace-nowrap">
                        <div className="mb-1.5 sm:hidden">{policySelect(book, "w-28")}</div>
                        {active ? (
                          active.state === "failed" ? (
                            <Link to="/downloads" className="text-sm text-red-600 hover:underline dark:text-red-400">
                              Failed
                            </Link>
                          ) : (
                            <Link to="/downloads" className="block w-24 text-left sm:w-36">
                              <Progress value={active.size ? active.bytes_done / active.size : 0} tone={active.state === "paused" ? "muted" : "brand"} />
                              <span className="muted mt-1 block text-xs capitalize">
                                {active.state}
                                {active.size > 0 && ` · ${Math.floor((active.bytes_done / active.size) * 100)}%`}
                              </span>
                            </Link>
                          )
                        ) : (
                          <button
                            className="btn w-full sm:w-auto"
                            disabled={busy}
                            onClick={() => run(() => post(`/api/updates/${encodeURIComponent(book.id)}/start`))}
                          >
                            Update
                          </button>
                        )}
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
