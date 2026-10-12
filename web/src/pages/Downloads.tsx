import { useState } from "react";
import { api, post, type Download } from "../lib/api";
import { bytes, duration } from "../lib/format";
import { useLive } from "../lib/live";
import { Empty, ErrorNote, PageHeader, Progress, Tabs } from "../ui";

const LABELS: Record<Download["state"], string> = {
  queued: "Waiting",
  downloading: "Downloading",
  verifying: "Verifying checksum",
  paused: "Paused",
  done: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
};

type Tab = "active" | "paused" | "failed" | "finished";

const TAB_OF: Record<Download["state"], Tab> = {
  queued: "active",
  downloading: "active",
  verifying: "active",
  paused: "paused",
  failed: "failed",
  done: "finished",
  cancelled: "finished",
};

const EMPTY: Record<Tab, [string, string]> = {
  active: ["Nothing is downloading", "Start a download from the Updates page or the Catalog."],
  paused: ["No paused downloads", "Downloads you pause wait here until you resume them."],
  failed: ["No failed downloads", "Downloads that stop with an error are listed here to retry."],
  finished: ["No finished downloads", "Completed and cancelled downloads are listed here."],
};

export default function Downloads() {
  const [tab, setTab] = useState<Tab>("active");
  const [sort, setSort] = useState("queue");
  const { downloads, refresh } = useLive();
  const [problem, setProblem] = useState<string | null>(null);
  const act = (request: Promise<unknown>) =>
    request.then(refresh).catch((reason) => setProblem(String(reason.message)));
  const action = (id: number, name: string) => act(post(`/api/downloads/${id}/${name}`));

  const running = downloads.filter((d) => d.state === "downloading");
  const speed = running.reduce((sum, d) => sum + d.speed, 0);
  const counts: Record<Tab, number> = { active: 0, paused: 0, failed: 0, finished: 0 };
  for (const d of downloads) counts[TAB_OF[d.state]] += 1;
  const shown = downloads.filter((d) => TAB_OF[d.state] === tab);
  const fraction = (d: Download) => (d.size ? d.bytes_done / d.size : 0);
  const orders: Record<string, (a: Download, b: Download) => number> = {
    title: (a, b) => a.title.localeCompare(b.title),
    size: (a, b) => b.size - a.size,
    "size-asc": (a, b) => a.size - b.size,
    progress: (a, b) => fraction(b) - fraction(a),
    recent: (a, b) => b.updated - a.updated,
  };
  if (orders[sort]) shown.sort(orders[sort]);

  return (
    <>
      <PageHeader
        title="Downloads"
        subtitle={running.length ? `${running.length} active · ${bytes(speed)}/s` : "Nothing is downloading"}
      >
        <select className="input w-auto flex-1 sm:flex-none" value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort by">
          <option value="queue">Queue order</option>
          <option value="recent">Most recent first</option>
          <option value="progress">Most complete first</option>
          <option value="title">Title A–Z</option>
          <option value="size">Largest first</option>
          <option value="size-asc">Smallest first</option>
        </select>
        {tab === "finished" && counts.finished > 0 && (
          <button className="btn" onClick={() => act(post("/api/downloads/clear-finished"))}>
            Clear finished
          </button>
        )}
      </PageHeader>
      <ErrorNote message={problem} />
      <Tabs<Tab>
        value={tab}
        onChange={setTab}
        options={[
          { value: "active", label: "Active", count: counts.active },
          { value: "paused", label: "Paused", count: counts.paused },
          { value: "failed", label: "Failed", count: counts.failed },
          { value: "finished", label: "Finished", count: counts.finished },
        ]}
      />
      {shown.length === 0 && <Empty title={EMPTY[tab][0]}>{EMPTY[tab][1]}</Empty>}
      <div className="space-y-2.5">
        {shown.map((d) => {
          const fraction = d.size ? d.bytes_done / d.size : 0;
          const live = d.state === "downloading";
          return (
            <div key={d.id} className="card p-4">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate font-medium">{d.title}</div>
                  <div className="muted truncate text-xs">
                    {d.filename || d.book_id}
                    {d.from_date ? ` · update from ${d.from_date}` : " · new book"}
                  </div>
                </div>
                <div className="flex gap-1.5">
                  {(d.state === "downloading" || d.state === "queued") && (
                    <button className="btn" onClick={() => action(d.id, "pause")}>
                      Pause
                    </button>
                  )}
                  {(d.state === "paused" || d.state === "failed") && (
                    <button className="btn" onClick={() => action(d.id, "resume")}>
                      {d.state === "failed" ? "Retry" : "Resume"}
                    </button>
                  )}
                  {d.state === "queued" && (
                    <button className="btn" onClick={() => action(d.id, "top")} title="Download this next">
                      Move to top
                    </button>
                  )}
                  {["queued", "downloading", "paused", "failed"].includes(d.state) && (
                    <button
                      className="btn btn-danger"
                      onClick={() =>
                        window.confirm(`Cancel ${d.title}? The ${bytes(d.bytes_done)} downloaded so far is discarded.`) &&
                        action(d.id, "cancel")
                      }
                    >
                      Cancel
                    </button>
                  )}
                  {["done", "cancelled"].includes(d.state) && (
                    <button className="btn" onClick={() => act(api(`/api/downloads/${d.id}`, { method: "DELETE" }))}>
                      Remove
                    </button>
                  )}
                </div>
              </div>
              {d.state !== "cancelled" && (
                <div className="mt-3">
                  <Progress
                    value={d.state === "done" ? 1 : fraction}
                    tone={d.state === "failed" ? "red" : live || d.state === "done" || d.state === "verifying" ? "brand" : "muted"}
                  />
                </div>
              )}
              <div className="muted mt-1.5 flex flex-wrap gap-x-4 text-xs">
                <span className={d.state === "failed" ? "text-red-600 dark:text-red-400" : ""}>
                  {LABELS[d.state]}
                  {d.error && `: ${d.error}`}
                </span>
                {d.size > 0 && (
                  <span>
                    {bytes(d.bytes_done)} of {bytes(d.size)} ({Math.floor(fraction * 100)}%)
                  </span>
                )}
                {live && (
                  <>
                    <span>{bytes(d.speed)}/s</span>
                    <span>
                      {d.connections} connection{d.connections === 1 ? "" : "s"}
                    </span>
                    {d.eta != null && <span>{duration(d.eta)} left</span>}
                  </>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}
