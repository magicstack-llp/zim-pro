import { useEffect, useRef, useState, type DragEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { Dialog } from "../Dialog";
import { api, authHeaders, post, readUrl, type Build } from "../lib/api";
import { ago, bytes, count } from "../lib/format";
import { useData, useLive } from "../lib/live";
import { Empty, ErrorNote, PageHeader, Progress, Tabs } from "../ui";
import { Segmented } from "./AiSettings";

interface LatestBuild {
  id: number;
  state: Build["state"];
  error: string | null;
  failed: number;
  skipped: number;
}
interface LinkRow {
  id: number;
  url: string;
  title: string;
  engine: "builtin" | "zimit";
  scope: "page" | "path" | "site";
  max_pages: number;
  max_depth: number;
  mode: "clean" | "full";
  images: boolean;
  video: boolean;
  subdomains: boolean;
  sitemaps: string;
  robots_url: string;
  rate_limit: number;
  rate_unit: "second" | "minute";
  feeds: string;
  feed_minutes: number;
  last_feed_check: number | null;
  language: string;
  notes: string;
  rebuild_days: number;
  book_id: string;
  last_built: number | null;
  book_title: string | null;
  book_size: number | null;
  book_pages: number | null;
  build: LatestBuild | null;
}
interface UploadRow {
  id: number;
  filename: string;
  kind: "zim" | "epub" | "pdf" | "mobi";
  size: number;
  title: string;
  images: boolean;
  book_id: string;
  last_built: number | null;
  book_title: string | null;
  book_size: number | null;
  build: LatestBuild | null;
}
type LinkDraft = Omit<LinkRow, "id" | "book_id" | "last_built" | "last_feed_check" | "book_title" | "book_size" | "book_pages" | "build"> & { id?: number };
interface CsvRow {
  row: number;
  url: string;
  title: string;
  status: "new" | "duplicate" | "invalid";
  why: string;
}

const NEW_LINK: LinkDraft = { url: "", title: "", engine: "builtin", scope: "page", max_pages: 500, max_depth: 5, mode: "clean", images: true, video: false, subdomains: false, sitemaps: "", robots_url: "", rate_limit: 0, rate_unit: "second", feeds: "", feed_minutes: 60, language: "", notes: "", rebuild_days: 0 };
const SCOPE_LABEL = { page: "This page only", path: "Pages under this address", site: "The whole site" };
export const PHASES: Record<string, string> = {
  "checking feeds": "Checking the feeds",
  "fetching articles": "Fetching new articles",
  assembling: "Putting the articles together",
  starting: "Starting",
  "reading the site's rules": "Reading robots.txt and sitemaps",
  fetching: "Fetching pages",
  "fetching files": "Fetching pictures and files",
  converting: "Converting",
  writing: "Writing the ZIM",
  verifying: "Checking the ZIM",
  "preparing the browser": "Preparing the browser",
  capturing: "Capturing pages",
};
/** Not over yet: waiting, working, or held by a pause. */
export const busy = (build: { state: Build["state"] } | null | undefined) => build?.state === "queued" || build?.state === "processing" || build?.state === "paused";

export function PauseButton({ job, act, path = "/api/build/jobs" }: { job: Build; act: (request: Promise<unknown>) => void; path?: string }) {
  const paused = job.state === "paused";
  return (
    <button className="btn" onClick={() => act(post(`${path}/${job.id}/${paused ? "resume" : "pause"}`))}>
      {paused ? "Resume" : "Pause"}
    </button>
  );
}

export function Switch({ checked, onChange, label }: { checked: boolean; onChange: (value: boolean) => void; label: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={`relative h-6 w-11 shrink-0 rounded-full transition ${checked ? "bg-brand-600" : "bg-zinc-300 dark:bg-zinc-700"}`}
    >
      <span className={`absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${checked ? "translate-x-5" : ""}`} />
    </button>
  );
}

export function Row({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 py-3">
      <div className="min-w-0 flex-1 basis-48">
        <div className="text-sm font-medium">{label}</div>
        {hint && <div className="muted text-xs">{hint}</div>}
      </div>
      {children}
    </div>
  );
}

const UNITS = { minutes: 1, hours: 60, days: 1440 } as const;
type Unit = keyof typeof UNITS;
/** 90 minutes stays "90 minutes"; 120 becomes "2 hours". */
export const splitInterval = (minutes: number): [number, Unit] => (minutes && minutes % 1440 === 0 ? [minutes / 1440, "days"] : minutes && minutes % 60 === 0 ? [minutes / 60, "hours"] : [minutes, "minutes"]);
export const every = (minutes: number) => {
  const [amount, unit] = splitInterval(minutes);
  return amount === 1 ? `every ${unit.slice(0, -1)}` : `every ${amount} ${unit}`;
};

/** How often: a number, and whether it counts minutes, hours or days. Kept as minutes. */
export function IntervalInput({ minutes, onChange }: { minutes: number; onChange: (minutes: number) => void }) {
  const [amount, unit] = splitInterval(minutes);
  return (
    <div className="flex items-center gap-2 text-sm">
      every
      <input className="input w-20" type="number" min={0} value={amount} onChange={(e) => onChange(Math.max(0, Math.round(Number(e.target.value) || 0)) * UNITS[unit])} aria-label="Interval" />
      <select className="input w-auto" value={unit} onChange={(e) => onChange(Math.max(1, amount) * UNITS[e.target.value as Unit])} aria-label="Interval unit">
        <option value="minutes">minutes</option>
        <option value="hours">hours</option>
        <option value="days">days</option>
      </select>
    </div>
  );
}

/** A rate limit: how many requests, and whether that is per second or per minute. */
export function RateInput({ amount, unit, onAmount, onUnit }: { amount: number; unit: "second" | "minute"; onAmount: (value: number) => void; onUnit: (value: "second" | "minute") => void }) {
  return (
    <div className="flex items-center gap-2 text-sm">
      <input className="input w-20" type="number" min={0} max={1000} step="any" value={amount} onChange={(e) => onAmount(Math.max(0, Number(e.target.value) || 0))} aria-label="Requests" />
      per
      <Segmented
        label="Rate unit"
        value={unit}
        onChange={onUnit}
        options={[
          { value: "second", label: "second" },
          { value: "minute", label: "minute" },
        ]}
      />
    </div>
  );
}

/** What a build is doing right now, or how its last run ended. */
function BuildState({ build, live }: { build: LatestBuild | null; live?: Build }) {
  if (live && busy(live)) {
    const fraction = live.total ? live.done / live.total : 0;
    return (
      <div className="mt-2">
        <div className="muted mb-1 flex justify-between text-xs">
          <span>{live.state === "queued" ? "Waiting to start" : live.state === "paused" ? "Paused" : live.detail || (PHASES[live.phase] ?? live.phase ?? "Working")}</span>
          {live.total > 0 && (
            <span>
              {count(live.done)} / {count(live.total)}
            </span>
          )}
        </div>
        <Progress value={live.started ? fraction : 0} />
      </div>
    );
  }
  if (!build) return null;
  if (build.state === "failed") return <p className="mt-1 text-xs text-red-600 dark:text-red-400">Failed: {build.error}</p>;
  if (build.state === "cancelled") return <p className="muted mt-1 text-xs">The last build was cancelled.</p>;
  if (build.state === "finished" && build.failed > 0)
    return (
      <p className="mt-1 text-xs text-amber-700 dark:text-amber-400">
        Finished, but {count(build.failed)} {build.failed === 1 ? "item" : "items"} could not be saved. See Jobs for the list.
      </p>
    );
  return null;
}

function LinkDialog({ initial, zimitUnavailable, onClose, onSaved }: { initial: LinkDraft; zimitUnavailable: string; onClose: () => void; onSaved: () => void }) {
  const [draft, setDraft] = useState(initial);
  const [problem, setProblem] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const set = <K extends keyof LinkDraft>(key: K, value: LinkDraft[K]) => setDraft({ ...draft, [key]: value });
  const zimit = draft.engine === "zimit";
  const save = () => {
    setSaving(true);
    setProblem(null);
    const { id, ...body } = draft;
    api(id ? `/api/build/links/${id}` : "/api/build/links", { method: id ? "PUT" : "POST", body: JSON.stringify(body) })
      .then(onSaved)
      .catch((reason) => setProblem(String(reason.message)))
      .finally(() => setSaving(false));
  };
  return (
    <Dialog
      title={draft.id ? "Edit link" : "Add a link"}
      onClose={onClose}
      footer={
        <div className="flex w-full justify-end gap-2">
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" onClick={save} disabled={saving || !draft.url.trim()}>
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      }
    >
      <ErrorNote message={problem} />
      <label className="block text-sm font-medium">
        Web address
        <input className="input mt-1" type="url" inputMode="url" placeholder="https://example.org/guide/" value={draft.url} onChange={(e) => set("url", e.target.value)} autoFocus />
      </label>
      <label className="mt-3 block text-sm font-medium">
        Title <span className="muted font-normal">(optional; the page's own title is used if empty)</span>
        <input className="input mt-1" value={draft.title} onChange={(e) => set("title", e.target.value)} />
      </label>
      <div className="mt-2 divide-y divide-zinc-100 dark:divide-zinc-800">
        <Row label="What to save" hint={SCOPE_LABEL[draft.scope]}>
          <Segmented
            label="What to save"
            value={draft.scope}
            onChange={(value) => set("scope", value)}
            options={[
              { value: "page", label: "Page" },
              { value: "path", label: "Section" },
              { value: "site", label: "Site" },
            ]}
          />
        </Row>
        {draft.scope === "site" && (
          <Row label="Include subdomains" hint="Also saves pages on addresses under this one, such as blog.example.org and docs.example.org for example.org.">
            <Switch checked={draft.subdomains} onChange={(value) => set("subdomains", value)} label="Include subdomains" />
          </Row>
        )}
        {draft.scope !== "page" && (
          <Row label="Limits" hint="Stop after this many pages, and this many links away from the first page.">
            <div className="flex items-center gap-2 text-sm">
              <input className="input w-24" type="number" min={1} max={50000} value={draft.max_pages} onChange={(e) => set("max_pages", Number(e.target.value) || 1)} aria-label="Most pages" />
              pages
              <input className="input w-16" type="number" min={0} max={20} value={draft.max_depth} onChange={(e) => set("max_depth", Number(e.target.value) || 0)} aria-label="Depth" />
              deep
            </div>
          </Row>
        )}
        <Row
          label="How to capture"
          hint={
            zimit
              ? "Zimit records pages in a real browser, exactly as shown. Slow and large; for sites that need JavaScript."
              : "Fetches pages directly. Fast and small; does not run JavaScript."
          }
        >
          <Segmented
            label="How to capture"
            value={draft.engine}
            onChange={(value) => set("engine", value)}
            options={[
              { value: "builtin", label: "Built-in" },
              { value: "zimit", label: "Browser (Zimit)" },
            ]}
          />
        </Row>
        {zimit && zimitUnavailable && (
          <p className="py-2 text-xs text-amber-700 dark:text-amber-400">
            Browser capture cannot run yet: {zimitUnavailable} The README explains how to enable it.
          </p>
        )}
        {!zimit && (
          <>
            <Row label="Page content" hint={draft.mode === "clean" ? "Keeps the article and drops menus, ads and scripts." : "Keeps the whole page and its styles; only scripts are removed."}>
              <Segmented
                label="Page content"
                value={draft.mode}
                onChange={(value) => set("mode", value)}
                options={[
                  { value: "clean", label: "Clean article" },
                  { value: "full", label: "Full page" },
                ]}
              />
            </Row>
            <Row label="Include pictures" hint="Off: pictures are replaced by their descriptions.">
              <Switch checked={draft.images} onChange={(value) => set("images", value)} label="Include pictures" />
            </Row>
            <Row label="Include video and audio" hint="Off: a link to the original is left in their place. These files are large.">
              <Switch checked={draft.video} onChange={(value) => set("video", value)} label="Include video and audio" />
            </Row>
          </>
        )}
        {!zimit && (
          <Row label="Rate limit" hint={draft.rate_limit > 0 ? `At most ${draft.rate_limit} ${draft.rate_limit === 1 ? "request" : "requests"} a ${draft.rate_unit} to the site.` : "0 means automatic: about 8 a second, or what the site's robots.txt asks for."}>
            <RateInput amount={draft.rate_limit} unit={draft.rate_unit} onAmount={(value) => set("rate_limit", value)} onUnit={(value) => set("rate_unit", value)} />
          </Row>
        )}
        <Row label="Rebuild the whole site" hint="Crawls everything again every so many days, which also picks up pages that changed. 0 means only when you ask.">
          <div className="flex items-center gap-2 text-sm">
            every
            <input className="input w-20" type="number" min={0} max={365} value={draft.rebuild_days} onChange={(e) => set("rebuild_days", Number(e.target.value) || 0)} aria-label="Days between rebuilds" />
            days
          </div>
        </Row>
      </div>
      {!zimit && (
        <label className="mt-2 block text-sm font-medium">
          Sitemaps{" "}
          <span className="muted font-normal">
            {draft.scope === "page" ? "(used when saving a section or a site; choose one above)" : "(optional, one address per line; empty: they are looked for)"}
          </span>
          <textarea className="input mt-1 h-20 font-mono text-xs" placeholder={"https://example.org/sitemap.xml\nhttps://example.org/news-sitemap.xml"} value={draft.sitemaps} onChange={(e) => set("sitemaps", e.target.value)} aria-label="Sitemaps" />
        </label>
      )}
      {!zimit && (
        <div className="mt-3 rounded-xl border border-zinc-200 p-3 dark:border-zinc-800">
          <label className="block text-sm font-medium">
            RSS feeds of this site <span className="muted font-normal">(optional, one address per line)</span>
            <textarea className="input mt-1 h-20 font-mono text-xs" placeholder={"https://example.org/feed.xml\nhttps://example.org/blog/atom.xml"} value={draft.feeds} onChange={(e) => set("feeds", e.target.value)} aria-label="RSS feeds" />
          </label>
          <p className="muted mt-1 text-xs">New articles the feeds list are added to the ZIM on their own schedule. Only those articles are fetched; the rest of the site is not crawled again.</p>
          {draft.feeds.trim() && (
            <Row label="Check the feeds" hint={draft.feed_minutes > 0 ? `Looks for new articles ${every(draft.feed_minutes)}. No more often than every 5 minutes.` : "0 means only when you press Check RSS."}>
              <IntervalInput minutes={draft.feed_minutes} onChange={(value) => set("feed_minutes", value)} />
            </Row>
          )}
        </div>
      )}
      {!zimit && (
        <label className="mt-2 block text-sm font-medium">
          robots.txt <span className="muted font-normal">(optional; empty: the site's own, at /robots.txt)</span>
          <input className="input mt-1 font-mono text-xs" type="url" inputMode="url" placeholder="https://example.org/robots.txt" value={draft.robots_url} onChange={(e) => set("robots_url", e.target.value)} aria-label="robots.txt address" />
        </label>
      )}
      <label className="mt-2 block text-sm font-medium">
        Notes <span className="muted font-normal">(optional)</span>
        <input className="input mt-1" value={draft.notes} onChange={(e) => set("notes", e.target.value)} />
      </label>
    </Dialog>
  );
}

function CsvDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<{ rows: CsvRow[]; counts: Record<CsvRow["status"], number> } | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const send = (commit: boolean) => {
    setWorking(true);
    setProblem(null);
    post("/api/build/links/import", { text, commit })
      .then((result) => (commit ? onDone() : setPreview(result)))
      .catch((reason) => setProblem(String(reason.message)))
      .finally(() => setWorking(false));
  };
  const tone = { new: "text-emerald-700 dark:text-emerald-400", duplicate: "muted", invalid: "text-red-600 dark:text-red-400" };
  return (
    <Dialog
      title="Import links from CSV"
      onClose={onClose}
      onBack={preview ? () => setPreview(null) : undefined}
      footer={
        preview ? (
          <button className="btn btn-primary" onClick={() => send(true)} disabled={working || !preview.counts.new}>
            {working ? "Importing…" : `Import ${preview.counts.new} new ${preview.counts.new === 1 ? "link" : "links"}`}
          </button>
        ) : (
          <button className="btn btn-primary" onClick={() => send(false)} disabled={working || !text.trim()}>
            {working ? "Checking…" : "Check the file"}
          </button>
        )
      }
    >
      <ErrorNote message={problem} />
      {!preview ? (
        <>
          <p className="muted text-sm">
            One link per row. Only <code>url</code> is required; optional columns are <code>title</code>, <code>scope</code> (page, path, site), <code>subdomains</code> (yes, no), <code>engine</code> (builtin, zimit),{" "}
            <code>mode</code> (clean, full), <code>images</code>, <code>video</code> (yes, no) and <code>max_pages</code>, <code>sitemaps</code>, <code>robots_url</code>, <code>rate_limit</code>, <code>rate_unit</code> (second, minute), <code>feeds</code> and <code>feed_minutes</code>. Nothing is added until you confirm.
          </p>
          <input
            className="mt-3 block w-full text-sm"
            type="file"
            accept=".csv,.tsv,.txt,text/csv"
            onChange={(e) => e.target.files?.[0]?.text().then(setText)}
            aria-label="CSV file"
          />
          <textarea
            className="input mt-3 h-40 font-mono text-xs"
            placeholder={"url,title,scope\nhttps://example.org/guide/,Example guide,path"}
            value={text}
            onChange={(e) => setText(e.target.value)}
            aria-label="CSV text"
          />
        </>
      ) : (
        <>
          <p className="text-sm">
            <strong>{preview.counts.new}</strong> new · {preview.counts.duplicate} already there · {preview.counts.invalid} cannot be read
          </p>
          <ul className="mt-3 max-h-80 divide-y divide-zinc-100 overflow-y-auto text-sm dark:divide-zinc-800">
            {preview.rows.map((row) => (
              <li key={row.row} className="py-2">
                <div className="truncate">{row.url || "(empty)"}</div>
                <div className={`text-xs ${tone[row.status]}`}>
                  Row {row.row}: {row.status === "new" ? "will be added" : row.why}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
    </Dialog>
  );
}

function Links({ live, onProblem }: { live: Build[]; onProblem: (message: string | null) => void }) {
  const { data, reload } = useData<{ links: LinkRow[]; zimit_unavailable: string }>("/api/build/links", [live.map((b) => b.state).join()]);
  const [dialog, setDialog] = useState<LinkDraft | null>(null);
  const [csv, setCsv] = useState(false);
  const act = (request: Promise<unknown>) => request.then(reload).catch((reason) => onProblem(String(reason.message)));
  const links = data?.links ?? [];
  const exportCsv = async () => {
    const reply = await fetch("/api/build/links/export", { headers: authHeaders() });
    const url = URL.createObjectURL(await reply.blob());
    const anchor = Object.assign(document.createElement("a"), { href: url, download: "zim-pro-links.csv" });
    anchor.click();
    URL.revokeObjectURL(url);
  };
  return (
    <>
      <div className="mb-3 flex flex-wrap gap-2">
        <button className="btn btn-primary" onClick={() => setDialog(NEW_LINK)}>
          Add link
        </button>
        <button className="btn" onClick={() => setCsv(true)}>
          Import CSV
        </button>
        {links.length > 0 && (
          <button className="btn" onClick={exportCsv}>
            Export CSV
          </button>
        )}
      </div>
      {data && links.length === 0 && (
        <Empty title="No links yet">Add a web address, or import a CSV of them, and make each one into a ZIM you can read, search and ask about offline.</Empty>
      )}
      <ul className="space-y-2">
        {links.map((link) => {
          const running = live.find((b) => b.kind === "link" && b.source_id === link.id && busy(b));
          const built = !!link.book_id && link.book_title !== null;
          return (
            <li key={link.id} className="card p-4">
              <div className="flex flex-wrap items-start gap-3">
                <div className="min-w-0 flex-1 basis-64">
                  <div className="truncate font-medium">{link.title || link.book_title || link.url}</div>
                  <a className="muted block truncate text-xs hover:underline" href={link.url} target="_blank" rel="noreferrer">
                    {link.url}
                  </a>
                  <div className="mt-1.5 flex flex-wrap gap-1.5">
                    <span className="badge">{SCOPE_LABEL[link.scope]}</span>
                    {link.scope === "site" && link.subdomains && <span className="badge">With subdomains</span>}
                    {link.engine === "zimit" ? (
                      <span className="badge">Browser capture</span>
                    ) : (
                      <>
                        <span className="badge">{link.mode === "clean" ? "Clean article" : "Full page"}</span>
                        <span className="badge">{link.images ? "Pictures" : "No pictures"}</span>
                        {link.video && <span className="badge">Video</span>}
                      </>
                    )}
                    {link.rate_limit > 0 && link.engine !== "zimit" && (
                      <span className="badge">
                        {link.rate_limit}/{link.rate_unit === "minute" ? "min" : "s"}
                      </span>
                    )}
                    {link.feeds && link.engine !== "zimit" && <span className="badge">{link.feed_minutes > 0 ? `RSS ${every(link.feed_minutes)}` : "RSS on request"}</span>}
                    {link.rebuild_days > 0 && <span className="badge">Rebuilt every {link.rebuild_days} days</span>}
                  </div>
                  {built ? (
                    <p className="muted mt-2 text-xs">
                      <Link className="text-brand-700 dark:text-brand-400 font-medium hover:underline" to={readUrl(link.book_id)}>
                        Open the ZIM
                      </Link>{" "}
                      · {count(link.book_pages ?? 0)} {link.book_pages === 1 ? "page" : "pages"} · {bytes(link.book_size ?? 0)} · made {ago(link.last_built)}
                      {link.feeds && link.last_feed_check ? ` · feeds checked ${ago(link.last_feed_check)}` : ""}
                    </p>
                  ) : (
                    !running && <p className="muted mt-2 text-xs">No ZIM has been made from this link yet.</p>
                  )}
                  <BuildState build={link.build} live={running} />
                </div>
                <div className="flex max-w-full flex-wrap gap-2 sm:shrink-0">
                  {running ? (
                    <>
                      <PauseButton job={running} act={act} />
                      <button className="btn" onClick={() => act(post(`/api/build/jobs/${running.id}/cancel`))}>
                        Cancel
                      </button>
                    </>
                  ) : (
                    <button className={`btn ${built ? "" : "btn-primary"}`} onClick={() => act(post(`/api/build/links/${link.id}/build`))}>
                      {built ? "Update" : "Create ZIM"}
                    </button>
                  )}
                  {!running && built && link.feeds && link.engine !== "zimit" && (
                    <button className="btn" title="Add the new articles the RSS feeds list, without crawling the site again" onClick={() => act(post(`/api/build/links/${link.id}/build?rss=true`))}>
                      Check RSS
                    </button>
                  )}
                  <button className="btn" onClick={() => setDialog(link)} disabled={!!running}>
                    Edit
                  </button>
                  <button
                    className="btn btn-danger"
                    disabled={!!running}
                    onClick={() => window.confirm(`Remove this link?${built ? " The ZIM made from it stays in your library." : ""}`) && act(api(`/api/build/links/${link.id}`, { method: "DELETE" }))}
                  >
                    Remove
                  </button>
                </div>
              </div>
            </li>
          );
        })}
      </ul>
      {dialog && (
        <LinkDialog
          initial={dialog}
          zimitUnavailable={data?.zimit_unavailable ?? ""}
          onClose={() => setDialog(null)}
          onSaved={() => {
            setDialog(null);
            reload();
          }}
        />
      )}
      {csv && (
        <CsvDialog
          onClose={() => setCsv(false)}
          onDone={() => {
            setCsv(false);
            reload();
          }}
        />
      )}
    </>
  );
}

const KIND_LABEL = { zim: "ZIM", epub: "EPUB", pdf: "PDF", mobi: "Kindle" };

function Uploads({ live, onProblem }: { live: Build[]; onProblem: (message: string | null) => void }) {
  const { data, reload } = useData<{ uploads: UploadRow[]; limit_mb: number }>("/api/build/uploads", [live.map((b) => b.state).join()]);
  const [sending, setSending] = useState<{ name: string; fraction: number } | null>(null);
  const [over, setOver] = useState(false);
  const [again, setAgain] = useState<UploadRow | null>(null);
  const picker = useRef<HTMLInputElement>(null);
  const act = (request: Promise<unknown>) => request.then(reload).catch((reason) => onProblem(String(reason.message)));

  // XMLHttpRequest, not fetch: it reports how much of the file has gone up.
  const send = (file: File) =>
    new Promise<void>((resolve, reject) => {
      const request = new XMLHttpRequest();
      request.open("POST", "/api/build/uploads");
      for (const [key, value] of Object.entries(authHeaders())) request.setRequestHeader(key, value);
      request.upload.onprogress = (event) => setSending({ name: file.name, fraction: event.total ? event.loaded / event.total : 0 });
      request.onload = () => {
        if (request.status < 300) return resolve();
        let detail = `HTTP ${request.status}`;
        try {
          detail = JSON.parse(request.responseText).detail ?? detail;
        } catch {
          /* not JSON */
        }
        reject(new Error(detail));
      };
      request.onerror = () => reject(new Error(`${file.name} could not be uploaded.`));
      const form = new FormData();
      form.append("file", file);
      request.send(form);
    });
  const sendAll = async (files: FileList | File[]) => {
    onProblem(null);
    for (const file of Array.from(files)) {
      setSending({ name: file.name, fraction: 0 });
      try {
        await send(file);
      } catch (reason) {
        onProblem(String((reason as Error).message));
      }
    }
    setSending(null);
    reload();
  };
  const drop = (event: DragEvent) => {
    event.preventDefault();
    setOver(false);
    if (event.dataTransfer.files.length) sendAll(event.dataTransfer.files);
  };
  const uploads = data?.uploads ?? [];
  return (
    <>
      <div
        onDragOver={(event) => {
          event.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={drop}
        className={`mb-4 rounded-2xl border-2 border-dashed px-6 py-8 text-center transition ${over ? "border-brand-500 bg-brand-50 dark:bg-brand-900/30" : "border-zinc-300 dark:border-zinc-700"}`}
      >
        <p className="font-medium">Drop books here to add them to your library</p>
        <p className="muted mx-auto mt-1 max-w-md text-sm">
          PDF, EPUB, MOBI and AZW3 books are converted to a ZIM, with the original kept inside. A ZIM file is added as it is.
          {data ? ` Up to ${bytes(data.limit_mb * 1024 * 1024)} each.` : ""}
        </p>
        <button className="btn btn-primary mt-4" onClick={() => picker.current?.click()} disabled={!!sending}>
          Choose files
        </button>
        <input ref={picker} type="file" multiple hidden accept=".zim,.pdf,.epub,.mobi,.azw3,.azw" onChange={(e) => e.target.files?.length && sendAll(e.target.files).then(() => (e.target.value = ""))} />
        {sending && (
          <div className="mx-auto mt-4 max-w-sm">
            <div className="muted mb-1 truncate text-xs">Uploading {sending.name}</div>
            <Progress value={sending.fraction} />
          </div>
        )}
      </div>
      <ul className="space-y-2">
        {uploads.map((upload) => {
          const running = live.find((b) => b.kind === "upload" && b.source_id === upload.id && busy(b));
          const built = !!upload.book_id && upload.book_title !== null;
          return (
            <li key={upload.id} className="card p-4">
              <div className="flex flex-wrap items-start gap-3">
                <div className="min-w-0 flex-1 basis-64">
                  <div className="truncate font-medium">{upload.book_title || upload.title || upload.filename}</div>
                  <div className="muted truncate text-xs">
                    {upload.filename} · {KIND_LABEL[upload.kind]} · {bytes(upload.size)}
                    {upload.kind !== "zim" && !upload.images ? " · without pictures" : ""}
                  </div>
                  {built && (
                    <p className="muted mt-2 text-xs">
                      <Link className="text-brand-700 dark:text-brand-400 font-medium hover:underline" to={readUrl(upload.book_id)}>
                        Open the ZIM
                      </Link>{" "}
                      · {bytes(upload.book_size ?? 0)} · {upload.kind === "zim" ? "added" : "converted"} {ago(upload.last_built)}
                    </p>
                  )}
                  <BuildState build={upload.build} live={running} />
                </div>
                <div className="flex max-w-full flex-wrap gap-2 sm:shrink-0">
                  {running ? (
                    <>
                      {upload.kind !== "zim" && <PauseButton job={running} act={act} />}
                      <button className="btn" onClick={() => act(post(`/api/build/jobs/${running.id}/cancel`))}>
                        Cancel
                      </button>
                    </>
                  ) : (
                    upload.kind !== "zim" && (
                      <button className="btn" onClick={() => setAgain(upload)}>
                        Convert again
                      </button>
                    )
                  )}
                  <button
                    className="btn btn-danger"
                    disabled={!!running}
                    onClick={() => window.confirm(`Remove ${upload.filename} from this list?${built ? " The ZIM made from it stays in your library." : ""}`) && act(api(`/api/build/uploads/${upload.id}`, { method: "DELETE" }))}
                  >
                    Remove
                  </button>
                </div>
              </div>
            </li>
          );
        })}
      </ul>
      {again && (
        <ConvertDialog
          upload={again}
          onClose={() => setAgain(null)}
          onDone={() => {
            setAgain(null);
            reload();
          }}
        />
      )}
    </>
  );
}

function ConvertDialog({ upload, onClose, onDone }: { upload: UploadRow; onClose: () => void; onDone: () => void }) {
  const [title, setTitle] = useState(upload.title);
  const [images, setImages] = useState(upload.images);
  const [problem, setProblem] = useState<string | null>(null);
  return (
    <Dialog
      title="Convert again"
      onClose={onClose}
      footer={
        <button className="btn btn-primary" onClick={() => post(`/api/build/uploads/${upload.id}/build`, { title, images }).then(onDone).catch((reason) => setProblem(String(reason.message)))}>
          Convert
        </button>
      }
    >
      <ErrorNote message={problem} />
      <p className="muted text-sm">{upload.filename} is converted again and replaces the ZIM made from it before.</p>
      <label className="mt-3 block text-sm font-medium">
        Title <span className="muted font-normal">(optional; the book's own title is used if empty)</span>
        <input className="input mt-1" value={title} onChange={(e) => setTitle(e.target.value)} />
      </label>
      <Row label="Include pictures" hint={upload.kind === "pdf" ? "Keeps a picture of every page beside its text. Off makes a much smaller ZIM, but scanned pages become unreadable." : "Off: the book's pictures are left out."}>
        <Switch checked={images} onChange={setImages} label="Include pictures" />
      </Row>
    </Dialog>
  );
}

type JobTab = "active" | "paused" | "finished" | "failed";
const JOB_TAB: Record<Build["state"], JobTab> = { queued: "active", processing: "active", paused: "paused", finished: "finished", cancelled: "finished", failed: "failed" };
const JOB_EMPTY: Record<JobTab, [string, string]> = {
  active: ["Nothing is being made", "Start from a link or upload a book."],
  paused: ["Nothing is paused", "A build you pause waits here, and carries on from where it stopped when you resume it."],
  finished: ["No finished builds", "ZIMs you make are listed here with what went into them."],
  failed: ["No failed builds", "Builds that stop with an error are listed here to retry."],
};

export function JobLog({ id, path = "/api/build/jobs" }: { id: number; path?: string }) {
  const { data } = useData<Build>(`${path}/${id}`);
  const log = data?.log ?? [];
  if (!data) return <p className="muted mt-2 text-xs">Loading…</p>;
  if (!log.length) return <p className="muted mt-2 text-xs">Everything was saved; nothing was skipped or failed.</p>;
  const tone = { failed: "text-red-600 dark:text-red-400", skipped: "text-amber-700 dark:text-amber-400", note: "muted" };
  return (
    <ul className="mt-2 max-h-64 space-y-1.5 overflow-y-auto rounded-xl bg-zinc-50 p-3 text-xs dark:bg-zinc-950">
      {log.map((entry, index) => (
        <li key={index}>
          <span className={`font-medium ${tone[entry.kind]}`}>{entry.kind === "note" ? "Note" : entry.kind === "failed" ? "Failed" : "Skipped"}</span>{" "}
          <span className="break-all">{entry.target}</span>
          <span className="muted"> — {entry.why}</span>
        </li>
      ))}
    </ul>
  );
}

function Jobs({ live, onProblem }: { live: Build[]; onProblem: (message: string | null) => void }) {
  const { refresh } = useLive();
  const [tab, setTab] = useState<JobTab>("active");
  const [open, setOpen] = useState<number | null>(null);
  const act = (request: Promise<unknown>) => request.then(refresh).catch((reason) => onProblem(String(reason.message)));
  const counts: Record<JobTab, number> = { active: 0, paused: 0, finished: 0, failed: 0 };
  for (const job of live) counts[JOB_TAB[job.state]] += 1;
  const shown = live.filter((job) => JOB_TAB[job.state] === tab);
  return (
    <>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <Tabs<JobTab>
          value={tab}
          onChange={setTab}
          options={[
            { value: "active", label: "Active", count: counts.active },
            { value: "paused", label: "Paused", count: counts.paused },
            { value: "finished", label: "Finished", count: counts.finished },
            { value: "failed", label: "Failed", count: counts.failed },
          ]}
        />
        {counts.finished + counts.failed > 0 && (
          <button className="btn" onClick={() => act(post("/api/build/jobs/clear-finished"))}>
            Clear finished
          </button>
        )}
      </div>
      {shown.length === 0 && <Empty title={JOB_EMPTY[tab][0]}>{JOB_EMPTY[tab][1]}</Empty>}
      <ul className="space-y-2">
        {shown.map((job) => {
          const working = busy(job);
          const fraction = job.total ? job.done / job.total : 0;
          return (
            <li key={job.id} className="card p-4">
              <div className="flex flex-wrap items-start gap-3">
                <div className="min-w-0 flex-1 basis-64">
                  <div className="truncate font-medium">{job.title}</div>
                  <div className="muted text-xs">
                    {job.kind === "upload" ? "From an uploaded file" : job.params?.rss ? (job.params.trigger === "schedule" ? "RSS update, on schedule" : "RSS update") : "From a link"} ·{" "}
                    {job.state === "queued"
                      ? "Waiting to start"
                      : job.state === "processing"
                        ? `Processing: ${(PHASES[job.phase] ?? job.phase ?? "working").toLowerCase()}`
                        : job.state === "paused"
                          ? job.started
                            ? `Paused while ${(PHASES[job.phase] ?? (job.phase || "working")).toLowerCase()}`
                            : "Paused before it started"
                        : job.state === "finished"
                          ? `Finished ${ago(job.finished)}`
                          : job.state === "failed"
                            ? `Failed ${ago(job.finished)}`
                            : `Cancelled ${ago(job.finished)}`}
                  </div>
                  {working && (
                    <div className="mt-2">
                      <Progress value={job.started ? fraction : 0} />
                      {job.state === "processing" && job.detail && <div className="mt-1 text-xs text-amber-700 dark:text-amber-400">Waiting: {job.detail}</div>}
                      <div className="muted mt-1 text-xs">
                        {job.total > 0 ? `${count(job.done)} of ${count(job.total)}` : job.state !== "paused" ? "Starting" : job.started ? "Paused" : "Not started"}
                        {job.failed > 0 ? ` · ${count(job.failed)} failed` : ""}
                        {job.skipped > 0 ? ` · ${count(job.skipped)} skipped` : ""}
                      </div>
                    </div>
                  )}
                  {job.state === "finished" && job.report.unchanged && <p className="mt-1.5 text-xs">Nothing new in the feeds; the ZIM was left as it was.</p>}
                  {job.state === "finished" && !job.report.unchanged && (
                    <p className="mt-1.5 text-xs">
                      {count(job.report.pages ?? job.done)} {(job.report.pages ?? job.done) === 1 ? "page" : "pages"}
                      {job.report.files ? ` · ${count(job.report.files)} files` : ""} · {bytes(job.size)}
                      {job.report.searchable === false ? " · not searchable" : ""}
                      {job.failed > 0 && <span className="text-amber-700 dark:text-amber-400"> · {count(job.failed)} failed</span>}
                      {job.skipped > 0 ? ` · ${count(job.skipped)} skipped` : ""}
                      {job.report.reused_from_cache ? ` · ${count(job.report.reused_from_cache)} unchanged since last time` : ""}
                      {job.report.from_feeds ? ` · ${count(job.report.from_feeds)} ${job.report.rss ? "new " : ""}from RSS` : ""}
                      {job.report.from_sitemaps ? ` · ${count(job.report.from_sitemaps)} found through sitemaps` : ""}
                      {job.report.slowed ? " · slowed down at the site's request" : ""}
                      {job.report.solved ? ` · ${count(job.report.solved)} browser ${job.report.solved === 1 ? "check" : "checks"} solved` : ""}
                    </p>
                  )}
                  {job.state === "failed" && <p className="mt-1.5 text-xs text-red-600 dark:text-red-400">{job.error}</p>}
                  {open === job.id && <JobLog id={job.id} />}
                </div>
                <div className="flex max-w-full flex-wrap gap-2 sm:shrink-0">
                  {job.state === "finished" && job.book_id && (
                    <Link className="btn" to={readUrl(job.book_id)}>
                      Open
                    </Link>
                  )}
                  <button className="btn" onClick={() => setOpen(open === job.id ? null : job.id)} aria-expanded={open === job.id}>
                    {open === job.id ? "Hide details" : "Details"}
                  </button>
                  {working ? (
                    <>
                      <PauseButton job={job} act={act} />
                      <button className="btn" onClick={() => act(post(`/api/build/jobs/${job.id}/cancel`))}>
                        Cancel
                      </button>
                    </>
                  ) : (
                    <>
                      {job.state !== "finished" && (
                        <button className="btn" onClick={() => act(post(`/api/build/jobs/${job.id}/retry`))}>
                          Retry
                        </button>
                      )}
                      <button className="btn" onClick={() => act(api(`/api/build/jobs/${job.id}`, { method: "DELETE" }))}>
                        Remove
                      </button>
                    </>
                  )}
                </div>
              </div>
            </li>
          );
        })}
      </ul>
    </>
  );
}

type Tab = "links" | "uploads" | "jobs";

export default function Create() {
  const { builds } = useLive();
  const [tab, setTab] = useState<Tab>("links");
  const [problem, setProblem] = useState<string | null>(null);
  const active = builds.filter((b) => b.state === "queued" || b.state === "processing").length;
  useEffect(() => setProblem(null), [tab]);
  return (
    <>
      <PageHeader title="Create" subtitle="Make your own ZIMs from web links and from books you upload." />
      <Tabs<Tab>
        value={tab}
        onChange={setTab}
        options={[
          { value: "links", label: "Links" },
          { value: "uploads", label: "Uploads" },
          { value: "jobs", label: "Jobs", count: active || undefined },
        ]}
      />
      <ErrorNote message={problem} />
      {tab === "links" && <Links live={builds} onProblem={setProblem} />}
      {tab === "uploads" && <Uploads live={builds} onProblem={setProblem} />}
      {tab === "jobs" && <Jobs live={builds} onProblem={setProblem} />}
    </>
  );
}
