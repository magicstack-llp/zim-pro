import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Dialog } from "../Dialog";
import { bookIconOf, CategorySelect, IconPicker, NO_ICON_CHANGE, ownIcon, saveIcon, sourceIcon } from "../Filing";
import { api, authHeaders, post, readUrl, type Build } from "../lib/api";
import { ago, bytes, categoryLabel, count, day } from "../lib/format";
import { useData, useLive } from "../lib/live";
import { BookIcon, Empty, ErrorNote, PageHeader, Progress, Tabs } from "../ui";
import { Segmented } from "./AiSettings";
import { busy, every, IntervalInput, JobLog, PauseButton, PHASES, RateInput, Row, Switch } from "./Create";

interface Source {
  id: number;
  url: string;
  title: string;
  site: string;
  last_checked: number | null;
  last_error: string | null;
}
type ItemState = "saved" | "new" | "failed" | "skipped" | "dropped";
interface FeedRow {
  id: number;
  title: string;
  language: string;
  notes: string;
  mode: "clean" | "full";
  images: boolean;
  video: boolean;
  full_text: boolean;
  interval_minutes: number;
  keep: number;
  enabled: boolean;
  rate_limit: number;
  rate_unit: "second" | "minute";
  category: string;
  icon: number | null;
  book_id: string;
  book_title: string | null;
  book_size: number | null;
  last_synced: number | null;
  last_new: number;
  last_error: string | null;
  next_sync: number | null;
  sources: Source[];
  counts: Partial<Record<ItemState, number>>;
}
interface Draft {
  id?: number;
  title: string;
  sources: string;
  interval_minutes: number;
  keep: number;
  full_text: boolean;
  mode: "clean" | "full";
  images: boolean;
  video: boolean;
  enabled: boolean;
  rate_limit: number;
  rate_unit: "second" | "minute";
  language: string;
  notes: string;
  category: string;
  icon?: number | null;
  book_id?: string;
  book_title?: string | null;
}
interface Found {
  url: string;
  title: string;
  articles: number;
  latest: string[];
  full_text: boolean;
}
interface Item {
  id: number;
  url: string;
  title: string;
  author: string;
  published: number;
  state: ItemState;
  error: string | null;
  path: string;
  source: string | null;
}
interface SettingsData {
  feed_default_interval: number;
  feed_default_keep: number;
}

const until = (timestamp: number) => {
  const seconds = timestamp - Date.now() / 1000;
  if (seconds < 45) return "any moment";
  if (seconds < 3600) return `in ${Math.round(seconds / 60)} min`;
  if (seconds < 86400 * 2) return `in ${Math.round(seconds / 3600)} h`;
  return `in ${Math.round(seconds / 86400)} days`;
};
const toDraft = (feed: FeedRow): Draft => ({
  id: feed.id,
  title: feed.title,
  sources: feed.sources.map((source) => source.url).join("\n"),
  interval_minutes: feed.interval_minutes,
  keep: feed.keep,
  full_text: feed.full_text,
  mode: feed.mode,
  images: feed.images,
  video: feed.video,
  enabled: feed.enabled,
  rate_limit: feed.rate_limit,
  rate_unit: feed.rate_unit,
  language: feed.language,
  notes: feed.notes,
  category: feed.category,
  icon: feed.icon,
  book_id: feed.book_id,
  book_title: feed.book_title,
});

function FeedDialog({ initial, onClose, onSaved }: { initial: Draft; onClose: () => void; onSaved: () => void }) {
  const [draft, setDraft] = useState(initial);
  const [problem, setProblem] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [lookup, setLookup] = useState("");
  const [looking, setLooking] = useState(false);
  const [found, setFound] = useState<Found[] | null>(null);
  const [icon, setIcon] = useState(NO_ICON_CHANGE);
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((current) => ({ ...current, [key]: value }));
  const lines = draft.sources.split("\n").map((line) => line.trim()).filter(Boolean);
  const add = (url: string) => !lines.includes(url) && set("sources", [...lines, url].join("\n"));
  const look = () => {
    setLooking(true);
    setProblem(null);
    setFound(null);
    post<{ feeds: Found[] }>("/api/feeds/check", { url: lookup })
      .then((result) => setFound(result.feeds))
      .catch((reason) => setProblem(String(reason.message)))
      .finally(() => setLooking(false));
  };
  const save = () => {
    setSaving(true);
    setProblem(null);
    const { id, icon: _icon, book_id: _book, book_title: _title, ...body } = draft;
    // A new collection is filled as soon as it is saved. With a picture to send first, the
    // server is asked to wait, and the first sync is started once the picture is there.
    const held = !id && !!icon.file;
    api<{ id: number }>(id ? `/api/feeds/${id}` : `/api/feeds${held ? "?hold=true" : ""}`, { method: id ? "PUT" : "POST", body: JSON.stringify(body) })
      .then(async (saved) => {
        await saveIcon("feed", saved.id, icon);
        if (held && body.enabled) await post(`/api/feeds/${saved.id}/sync`);
      })
      .then(onSaved)
      .catch((reason) => setProblem(String(reason.message)))
      .finally(() => setSaving(false));
  };
  return (
    <Dialog
      title={draft.id ? "Edit collection" : "New feed collection"}
      onClose={onClose}
      footer={
        <div className="flex w-full justify-end gap-2">
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" onClick={save} disabled={saving || !lines.length}>
            {saving ? "Saving…" : draft.id ? "Save" : "Save and sync"}
          </button>
        </div>
      }
    >
      <ErrorNote message={problem} />
      <label className="block text-sm font-medium">
        Name <span className="muted font-normal">(optional; the first feed's name is used if empty)</span>
        <input className="input mt-1" value={draft.title} onChange={(e) => set("title", e.target.value)} placeholder="Morning news" />
      </label>
      <label className="mt-3 block text-sm font-medium">
        Feed addresses <span className="muted font-normal">(RSS or Atom, one per line; all of them go into one ZIM)</span>
        <textarea
          className="input mt-1 h-24 font-mono text-xs"
          placeholder={"https://example.org/feed.xml\nhttps://blog.example.com/atom.xml"}
          value={draft.sources}
          onChange={(e) => set("sources", e.target.value)}
          aria-label="Feed addresses"
          autoFocus
        />
      </label>
      <div className="mt-2 flex gap-2">
        <input
          className="input flex-1 text-sm"
          placeholder="Find a site's feeds: example.org"
          value={lookup}
          onChange={(e) => setLookup(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && lookup.trim() && look()}
          aria-label="Find feeds at an address"
        />
        <button className="btn" onClick={look} disabled={looking || !lookup.trim()}>
          {looking ? "Looking…" : "Find feeds"}
        </button>
      </div>
      {found && (
        <ul className="mt-2 space-y-1.5">
          {found.map((feed) => (
            <li key={feed.url} className="flex items-start gap-2 rounded-xl bg-zinc-50 p-2.5 text-sm dark:bg-zinc-950">
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium">{feed.title || feed.url}</div>
                <div className="muted truncate text-xs">
                  {feed.url} · {count(feed.articles)} {feed.articles === 1 ? "article" : "articles"} · {feed.full_text ? "full text in the feed" : "summaries only"}
                </div>
                {feed.latest[0] && <div className="muted truncate text-xs">Latest: {feed.latest[0]}</div>}
              </div>
              <button className="btn shrink-0" onClick={() => add(feed.url)} disabled={lines.includes(feed.url)}>
                {lines.includes(feed.url) ? "Added" : "Add"}
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="mt-2 divide-y divide-zinc-100 dark:divide-zinc-800">
        <Row label="Category" hint="Where the ZIM is filed in the Library, in Search and in Ask AI.">
          <CategorySelect value={draft.category} onChange={(value) => set("category", value)} />
        </Row>
        <Row label="Picture" hint="">
          <IconPicker own={ownIcon("feed", draft)} fallback={bookIconOf(draft)} title={draft.title || "Feeds"} choice={icon} onChange={setIcon} hint={draft.book_id ? "Yours replaces the site's own icon the next time the ZIM is written." : "Empty: the first site's own icon is used, or a tile with the title's first letter."} />
        </Row>
        <Row label="Sync on schedule" hint={draft.enabled && draft.interval_minutes > 0 ? `Checked for new articles ${every(draft.interval_minutes)}.` : "Only when you press Sync."}>
          <Switch checked={draft.enabled} onChange={(value) => set("enabled", value)} label="Sync on schedule" />
        </Row>
        {draft.enabled && (
          <Row label="How often" hint="No more often than every 5 minutes. 0 means only when you press Sync.">
            <IntervalInput minutes={draft.interval_minutes} onChange={(value) => set("interval_minutes", value)} />
          </Row>
        )}
        <Row label="Articles to keep" hint={draft.keep > 0 ? `The newest ${count(draft.keep)} stay in the ZIM; older ones drop out.` : "0 keeps every article, so the ZIM keeps growing."}>
          <input className="input w-24" type="number" min={0} value={draft.keep} onChange={(e) => set("keep", Math.max(0, Math.round(Number(e.target.value) || 0)))} aria-label="Articles to keep" />
        </Row>
        <Row label="Fetch the full article" hint={draft.full_text ? "Opens each article's page and saves it. The feed's own text is used if the page cannot be had." : "Saves only what the feed carries: fast, but often just a summary."}>
          <Switch checked={draft.full_text} onChange={(value) => set("full_text", value)} label="Fetch the full article" />
        </Row>
        {draft.full_text && (
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
        )}
        <Row label="Include pictures" hint="Off: pictures are replaced by their descriptions.">
          <Switch checked={draft.images} onChange={(value) => set("images", value)} label="Include pictures" />
        </Row>
        <Row label="Include video and audio" hint="Off: a link to the original is left in their place. Podcast files are large.">
          <Switch checked={draft.video} onChange={(value) => set("video", value)} label="Include video and audio" />
        </Row>
        <Row label="Rate limit" hint={draft.rate_limit > 0 ? `At most ${draft.rate_limit} ${draft.rate_limit === 1 ? "request" : "requests"} a ${draft.rate_unit} to each site.` : "0 means automatic."}>
          <RateInput amount={draft.rate_limit} unit={draft.rate_unit} onAmount={(value) => set("rate_limit", value)} onUnit={(value) => set("rate_unit", value)} />
        </Row>
      </div>
      <label className="mt-2 block text-sm font-medium">
        Notes <span className="muted font-normal">(optional)</span>
        <input className="input mt-1" value={draft.notes} onChange={(e) => set("notes", e.target.value)} />
      </label>
    </Dialog>
  );
}

function OpmlDialog({ onClose, onDone }: { onClose: () => void; onDone: (message: string) => void }) {
  const [text, setText] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const send = () => {
    setWorking(true);
    setProblem(null);
    post<{ added: number; collections: number }>("/api/feeds/opml", { text })
      .then((result) => onDone(`Added ${result.added} ${result.added === 1 ? "feed" : "feeds"} in ${result.collections} ${result.collections === 1 ? "collection" : "collections"}. Press Sync to fill them.`))
      .catch((reason) => setProblem(String(reason.message)))
      .finally(() => setWorking(false));
  };
  return (
    <Dialog
      title="Import feeds from OPML"
      onClose={onClose}
      footer={
        <button className="btn btn-primary" onClick={send} disabled={working || !text.trim()}>
          {working ? "Importing…" : "Import"}
        </button>
      }
    >
      <ErrorNote message={problem} />
      <p className="muted text-sm">OPML is the file other feed readers export. Each folder in it becomes one collection, and so one ZIM; feeds outside a folder go into "Imported feeds".</p>
      <input className="mt-3 block w-full text-sm" type="file" accept=".opml,.xml,text/xml" onChange={(e) => e.target.files?.[0]?.text().then(setText)} aria-label="OPML file" />
      <textarea className="input mt-3 h-40 font-mono text-xs" placeholder="<opml>…</opml>" value={text} onChange={(e) => setText(e.target.value)} aria-label="OPML text" />
    </Dialog>
  );
}

const ITEM_LABEL: Record<ItemState, string> = { saved: "Saved", new: "Waiting", failed: "Failed", skipped: "Skipped", dropped: "Dropped" };
const ITEM_TONE: Record<ItemState, string> = {
  saved: "text-emerald-700 dark:text-emerald-400",
  new: "muted",
  failed: "text-red-600 dark:text-red-400",
  skipped: "text-amber-700 dark:text-amber-400",
  dropped: "muted",
};

/** The articles of one collection, with the feeds they come from. */
function Articles({ feed, revision }: { feed: FeedRow; revision: string }) {
  const [state, setState] = useState<ItemState | "">("");
  const [query, setQuery] = useState("");
  const [shown, setShown] = useState(30);
  const { data } = useData<{ items: Item[]; total: number }>(`/api/feeds/${feed.id}/items?count=${shown}&state=${state}&q=${encodeURIComponent(query)}`, [revision]);
  const filters = (["saved", "new", "failed", "skipped", "dropped"] as const).filter((key) => feed.counts[key]);
  return (
    <div className="mt-3 rounded-xl bg-zinc-50 p-3 dark:bg-zinc-950">
      <div className="text-xs font-medium">Feeds</div>
      <ul className="mt-1 space-y-1 text-xs">
        {feed.sources.map((source) => (
          <li key={source.id} className="min-w-0">
            <span className="font-medium">{source.title || "Not read yet"}</span>{" "}
            <a className="muted break-all hover:underline" href={source.url} target="_blank" rel="noreferrer">
              {source.url}
            </a>
            {source.last_error ? <span className="text-red-600 dark:text-red-400"> — {source.last_error}</span> : <span className="muted"> — checked {ago(source.last_checked)}</span>}
          </li>
        ))}
      </ul>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <div className="text-xs font-medium">Articles</div>
        <select className="input w-auto py-1 text-xs" value={state} onChange={(e) => setState(e.target.value as ItemState | "")} aria-label="Show articles">
          <option value="">All</option>
          {filters.map((key) => (
            <option key={key} value={key}>
              {ITEM_LABEL[key]} ({count(feed.counts[key] ?? 0)})
            </option>
          ))}
        </select>
        <input className="input w-40 flex-1 py-1 text-xs sm:flex-none" placeholder="Filter by title" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Filter articles by title" />
      </div>
      {data && data.items.length === 0 && <p className="muted mt-2 text-xs">No articles here yet.</p>}
      <ul className="mt-2 max-h-96 space-y-1.5 overflow-y-auto text-xs">
        {data?.items.map((item) => (
          <li key={item.id} className="min-w-0">
            {item.state === "saved" && feed.book_id && feed.book_title !== null ? (
              <Link className="text-brand-700 dark:text-brand-400 font-medium hover:underline" to={readUrl(feed.book_id, item.path)}>
                {item.title}
              </Link>
            ) : (
              <span className="font-medium">{item.title}</span>
            )}
            <span className="muted">
              {" "}
              · {day(item.published)}
              {feed.sources.length > 1 && item.source ? ` · ${item.source}` : ""}
            </span>
            {item.state !== "saved" && <span className={ITEM_TONE[item.state]}> · {ITEM_LABEL[item.state]}</span>}
            {item.error && <span className="muted"> — {item.error}</span>}
            {item.url && (
              <a className="muted ml-1 hover:underline" href={item.url} target="_blank" rel="noreferrer" aria-label={`Open ${item.title} on the web`}>
                ↗
              </a>
            )}
          </li>
        ))}
      </ul>
      {data && data.total > data.items.length && (
        <button className="btn mt-2" onClick={() => setShown(shown + 100)}>
          Show more ({count(data.total - data.items.length)} left)
        </button>
      )}
    </div>
  );
}

function Collections({ runs, onProblem }: { runs: Build[]; onProblem: (message: string | null) => void }) {
  const revision = runs.map((run) => `${run.id}${run.state}`).join();
  const { data, reload } = useData<{ feeds: FeedRow[]; auto_sync: boolean }>("/api/feeds", [revision]);
  const settings = useData<SettingsData>("/api/settings");
  const [dialog, setDialog] = useState<Draft | null>(null);
  const [opml, setOpml] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const act = (request: Promise<unknown>) => {
    onProblem(null);
    return request.then(reload).catch((reason) => onProblem(String(reason.message)));
  };
  const feeds = data?.feeds ?? [];
  const blank: Draft = {
    title: "",
    sources: "",
    interval_minutes: settings.data?.feed_default_interval ?? 60,
    keep: settings.data?.feed_default_keep ?? 0,
    full_text: true,
    mode: "clean",
    images: true,
    video: false,
    enabled: true,
    rate_limit: 0,
    rate_unit: "second",
    language: "",
    notes: "",
    category: "",
  };
  const exportOpml = async () => {
    const reply = await fetch("/api/feeds/opml", { headers: authHeaders() });
    const url = URL.createObjectURL(await reply.blob());
    Object.assign(document.createElement("a"), { href: url, download: "zim-pro-feeds.opml" }).click();
    URL.revokeObjectURL(url);
  };
  return (
    <>
      <div className="mb-3 flex flex-wrap gap-2">
        <button className="btn btn-primary" onClick={() => setDialog(blank)}>
          New collection
        </button>
        <button className="btn" onClick={() => setOpml(true)}>
          Import OPML
        </button>
        {feeds.length > 0 && (
          <>
            <button className="btn" onClick={exportOpml}>
              Export OPML
            </button>
            <button className="btn" onClick={() => act(post("/api/feeds/sync-all"))}>
              Sync all
            </button>
          </>
        )}
      </div>
      {note && <p className="mb-3 text-sm text-emerald-700 dark:text-emerald-400">{note}</p>}
      {data && !data.auto_sync && feeds.length > 0 && (
        <p className="mb-3 text-sm text-amber-700 dark:text-amber-400">
          Scheduled syncing is switched off in <Link className="underline" to="/settings">Settings</Link>. Collections only sync when you press Sync.
        </p>
      )}
      {data && feeds.length === 0 && (
        <Empty title="No feed collections yet">A collection is one or more RSS or Atom feeds kept as a single ZIM. New articles are added on a schedule, so the news, blogs and release notes you follow can be read, searched and asked about offline.</Empty>
      )}
      <ul className="space-y-2">
        {feeds.map((feed) => {
          const running = runs.find((run) => run.source_id === feed.id && busy(run));
          const built = !!feed.book_id && feed.book_title !== null;
          const saved = feed.counts.saved ?? 0;
          const failed = feed.counts.failed ?? 0;
          const scheduled = feed.enabled && feed.interval_minutes > 0;
          const fraction = running?.total ? running.done / running.total : 0;
          return (
            <li key={feed.id} className="card p-4">
              <div className="flex flex-wrap items-start gap-3">
                <BookIcon key={sourceIcon("feed", feed) ?? "none"} src={sourceIcon("feed", feed)} title={feed.title || feed.sources[0]?.title || "Feeds"} size={40} />
                <div className="min-w-0 flex-1 basis-64">
                  <div className="truncate font-medium">{feed.title || feed.sources[0]?.title || feed.sources[0]?.url || "Empty collection"}</div>
                  <div className="muted truncate text-xs">{feed.sources.length === 1 ? feed.sources[0].url : `${feed.sources.length} feeds: ${feed.sources.map((source) => source.title || new URL(source.url).hostname).join(", ")}`}</div>
                  <div className="mt-1.5 flex flex-wrap gap-1.5">
                    {feed.category && <span className="badge bg-brand-50 text-brand-700 dark:bg-brand-900/50 dark:text-brand-100">{categoryLabel(feed.category)}</span>}
                    <span className="badge">{scheduled ? `Syncs ${every(feed.interval_minutes)}` : "Manual sync"}</span>
                    <span className="badge">{feed.full_text ? (feed.mode === "clean" ? "Full article, cleaned" : "Full page") : "Feed text only"}</span>
                    <span className="badge">{feed.keep > 0 ? `Keeps ${count(feed.keep)}` : "Keeps everything"}</span>
                    {!feed.images && <span className="badge">No pictures</span>}
                    {feed.video && <span className="badge">Video</span>}
                    {feed.rate_limit > 0 && (
                      <span className="badge">
                        {feed.rate_limit}/{feed.rate_unit === "minute" ? "min" : "s"}
                      </span>
                    )}
                  </div>
                  <p className="muted mt-2 text-xs">
                    {built && (
                      <>
                        <Link className="text-brand-700 dark:text-brand-400 font-medium hover:underline" to={readUrl(feed.book_id)}>
                          Open the ZIM
                        </Link>{" "}
                        · {bytes(feed.book_size ?? 0)} ·{" "}
                      </>
                    )}
                    {count(saved)} {saved === 1 ? "article" : "articles"}
                    {failed > 0 && <span className="text-amber-700 dark:text-amber-400"> · {count(failed)} failed</span>} · {feed.last_synced ? `synced ${ago(feed.last_synced)}${feed.last_new ? `, ${count(feed.last_new)} new` : ", nothing new"}` : "never synced"}
                    {scheduled && data?.auto_sync && feed.next_sync && !running ? ` · next ${until(feed.next_sync)}` : ""}
                  </p>
                  {feed.last_error && !running && <p className="mt-1 text-xs text-red-600 dark:text-red-400">Last sync failed: {feed.last_error}</p>}
                  {running && (
                    <div className="mt-2">
                      <div className="muted mb-1 flex justify-between text-xs">
                        <span>{running.state === "queued" ? "Waiting to start" : running.state === "paused" ? "Paused" : running.detail || (PHASES[running.phase] ?? running.phase ?? "Working")}</span>
                        {running.total > 0 && (
                          <span>
                            {count(running.done)} / {count(running.total)}
                          </span>
                        )}
                      </div>
                      <Progress value={running.started ? fraction : 0} />
                    </div>
                  )}
                  {open === feed.id && <Articles feed={feed} revision={revision} />}
                </div>
                <div className="flex max-w-full flex-wrap gap-2 sm:shrink-0">
                  {running ? (
                    <>
                      <PauseButton job={running} act={act} path="/api/feeds/runs" />
                      <button className="btn" onClick={() => act(post(`/api/feeds/runs/${running.id}/cancel`))}>
                        Cancel
                      </button>
                    </>
                  ) : (
                    <button className={`btn ${built ? "" : "btn-primary"}`} onClick={() => act(post(`/api/feeds/${feed.id}/sync`))}>
                      Sync now
                    </button>
                  )}
                  <button className="btn" onClick={() => setOpen(open === feed.id ? null : feed.id)} aria-expanded={open === feed.id}>
                    {open === feed.id ? "Hide articles" : "Articles"}
                  </button>
                  {!running && failed > 0 && (
                    <button className="btn" onClick={() => act(post(`/api/feeds/${feed.id}/retry-failed`))}>
                      Retry failed
                    </button>
                  )}
                  {!running && built && (
                    <button className="btn" title="Write the ZIM again from everything saved, and ask again for articles that failed" onClick={() => act(post(`/api/feeds/${feed.id}/sync`, { rebuild: true }))}>
                      Rebuild
                    </button>
                  )}
                  <button className="btn" onClick={() => setDialog(toDraft(feed))} disabled={!!running}>
                    Edit
                  </button>
                  <button
                    className="btn btn-danger"
                    disabled={!!running}
                    onClick={() => window.confirm(`Remove this collection and its saved articles?${built ? " The ZIM made from it stays in your library." : ""}`) && act(api(`/api/feeds/${feed.id}`, { method: "DELETE" }))}
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
        <FeedDialog
          initial={dialog}
          onClose={() => setDialog(null)}
          onSaved={() => {
            setDialog(null);
            reload();
          }}
        />
      )}
      {opml && (
        <OpmlDialog
          onClose={() => setOpml(false)}
          onDone={(message) => {
            setOpml(false);
            setNote(message);
            reload();
          }}
        />
      )}
    </>
  );
}

type RunTab = "active" | "paused" | "finished" | "failed";
const RUN_TAB: Record<Build["state"], RunTab> = { queued: "active", processing: "active", paused: "paused", finished: "finished", cancelled: "finished", failed: "failed" };
const RUN_EMPTY: Record<RunTab, [string, string]> = {
  active: ["Nothing is syncing", "Syncs start on each collection's schedule, or when you press Sync."],
  paused: ["Nothing is paused", "A sync you pause waits here and carries on when you resume it."],
  finished: ["No finished syncs", "The last hundred syncs are listed here with what each one found."],
  failed: ["No failed syncs", "Syncs that stop with an error are listed here to retry."],
};

function Runs({ runs, onProblem }: { runs: Build[]; onProblem: (message: string | null) => void }) {
  const { refresh } = useLive();
  const [tab, setTab] = useState<RunTab>("active");
  const [open, setOpen] = useState<number | null>(null);
  const act = (request: Promise<unknown>) => request.then(refresh).catch((reason) => onProblem(String(reason.message)));
  const counts: Record<RunTab, number> = { active: 0, paused: 0, finished: 0, failed: 0 };
  for (const run of runs) counts[RUN_TAB[run.state]] += 1;
  const shown = runs.filter((run) => RUN_TAB[run.state] === tab);
  return (
    <>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <Tabs<RunTab>
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
          <button className="btn" onClick={() => act(post("/api/feeds/runs/clear-finished"))}>
            Clear finished
          </button>
        )}
      </div>
      {shown.length === 0 && <Empty title={RUN_EMPTY[tab][0]}>{RUN_EMPTY[tab][1]}</Empty>}
      <ul className="space-y-2">
        {shown.map((run) => {
          const working = busy(run);
          const fraction = run.total ? run.done / run.total : 0;
          const report = run.report;
          return (
            <li key={run.id} className="card p-4">
              <div className="flex flex-wrap items-start gap-3">
                <div className="min-w-0 flex-1 basis-64">
                  <div className="truncate font-medium">{run.title}</div>
                  <div className="muted text-xs">
                    {run.params?.trigger === "schedule" ? "On schedule" : run.params?.rebuild ? "Rebuild" : "Started by you"} ·{" "}
                    {run.state === "queued"
                      ? "Waiting to start"
                      : run.state === "processing"
                        ? (PHASES[run.phase] ?? run.phase ?? "Working")
                        : run.state === "paused"
                          ? "Paused"
                          : run.state === "finished"
                            ? `Finished ${ago(run.finished)}`
                            : run.state === "failed"
                              ? `Failed ${ago(run.finished)}`
                              : `Cancelled ${ago(run.finished)}`}
                  </div>
                  {working && (
                    <div className="mt-2">
                      <Progress value={run.started ? fraction : 0} />
                      {run.state === "processing" && run.detail && <div className="mt-1 text-xs text-amber-700 dark:text-amber-400">Waiting: {run.detail}</div>}
                      <div className="muted mt-1 text-xs">
                        {run.total > 0 ? `${count(run.done)} of ${count(run.total)}` : ""}
                        {run.failed > 0 ? ` · ${count(run.failed)} failed` : ""}
                      </div>
                    </div>
                  )}
                  {run.state === "finished" && (
                    <p className="mt-1.5 text-xs">
                      {report.new ? `${count(report.new)} new ${report.new === 1 ? "article" : "articles"}` : "Nothing new"}
                      {report.articles !== undefined ? ` · ${count(report.articles)} in the ZIM` : ""}
                      {report.unchanged ? " · ZIM left as it was" : run.size ? ` · ${bytes(run.size)}` : ""}
                      {report.dropped ? ` · ${count(report.dropped)} old ones dropped` : ""}
                      {run.failed > 0 && <span className="text-amber-700 dark:text-amber-400"> · {count(run.failed)} failed</span>}
                      {report.slowed ? " · slowed down at a site's request" : ""}
                      {report.solved ? ` · ${count(report.solved)} browser ${report.solved === 1 ? "check" : "checks"} solved` : ""}
                      {report.address_changes ? ` · VPN address changed ${report.address_changes === 1 ? "once" : `${count(report.address_changes)} times`}` : ""}
                    </p>
                  )}
                  {run.state === "failed" && <p className="mt-1.5 text-xs text-red-600 dark:text-red-400">{run.error}</p>}
                  {open === run.id && <JobLog id={run.id} path="/api/feeds/runs" />}
                </div>
                <div className="flex max-w-full flex-wrap gap-2 sm:shrink-0">
                  {run.state === "finished" && run.book_id && (
                    <Link className="btn" to={readUrl(run.book_id)}>
                      Open
                    </Link>
                  )}
                  <button className="btn" onClick={() => setOpen(open === run.id ? null : run.id)} aria-expanded={open === run.id}>
                    {open === run.id ? "Hide details" : "Details"}
                  </button>
                  {working ? (
                    <>
                      <PauseButton job={run} act={act} path="/api/feeds/runs" />
                      <button className="btn" onClick={() => act(post(`/api/feeds/runs/${run.id}/cancel`))}>
                        Cancel
                      </button>
                    </>
                  ) : (
                    <>
                      {run.state !== "finished" && (
                        <button className="btn" onClick={() => act(post(`/api/feeds/runs/${run.id}/retry`))}>
                          Retry
                        </button>
                      )}
                      <button className="btn" onClick={() => act(api(`/api/feeds/runs/${run.id}`, { method: "DELETE" }))}>
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

type Tab = "collections" | "runs";

export default function Feeds() {
  const { feeds: runs } = useLive();
  const [tab, setTab] = useState<Tab>("collections");
  const [problem, setProblem] = useState<string | null>(null);
  const active = runs.filter((run) => run.state === "queued" || run.state === "processing").length;
  useEffect(() => setProblem(null), [tab]);
  return (
    <>
      <PageHeader title="Feeds" subtitle="ZIMs kept up to date from RSS and Atom feeds: only new articles are fetched." />
      <Tabs<Tab>
        value={tab}
        onChange={setTab}
        options={[
          { value: "collections", label: "Collections" },
          { value: "runs", label: "Syncs", count: active || undefined },
        ]}
      />
      <ErrorNote message={problem} />
      {tab === "collections" && <Collections runs={runs} onProblem={setProblem} />}
      {tab === "runs" && <Runs runs={runs} onProblem={setProblem} />}
    </>
  );
}
