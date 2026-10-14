import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, post } from "../lib/api";
import { ago, bytes, count } from "../lib/format";
import { useData } from "../lib/live";
import { ErrorNote } from "../ui";

/** The parts of Settings that stand on their own: its sections menu, MCP keys and About. */

export const TABS = [
  { id: "general", label: "General", about: "Downloads, updates, search, library folder", icon: "M4 6h7M15 6h1M4 14h1M9 14h7M13 4v4M7 12v4" },
  { id: "ai", label: "AI", about: "Providers, Ask AI, web search, voice", icon: "M10 3l1.6 4.4L16 9l-4.4 1.6L10 15l-1.6-4.4L4 9l4.4-1.6zM15.5 13.5l.6 1.4 1.4.6-1.4.6-.6 1.4-.6-1.4-1.4-.6 1.4-.6z" },
  { id: "create", label: "Create and Feeds", about: "Builds, uploads, feed syncs", icon: "M10 4.5v11M4.5 10h11M4 4h12v12H4z" },
  { id: "network", label: "Network", about: "VPN and captcha solver", icon: "M10 17a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM3 10h14M10 3c2 2.2 3 4.5 3 7s-1 4.800-3 7c-2-2.200-3-4.500-3-7s1-4.800 3-7z" },
  { id: "queues", label: "Queues", about: "Downloads, builds and feed syncs", icon: "M4 5h12M4 10h12M4 15h7M15 13v4M13 15h4" },
  { id: "monitoring", label: "Monitoring", about: "Health, resources, traffic", icon: "M3 10h3l2-5 4 10 2-5h3" },
  { id: "logs", label: "Logs", about: "Everything the server did", icon: "M5 3h7l3 3v11H5zM8 9h5M8 12h5M8 15h3" },
  { id: "mcp", label: "MCP", about: "Access for LLM apps, API keys", icon: "M8 6 4 10l4 4M12 6l4 4-4 4" },
  { id: "about", label: "About", about: "Version and links", icon: "M10 17a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM10 9.500v4M10 6.700h.01" },
] as const;
export type TabId = (typeof TABS)[number]["id"];
/** Links from elsewhere in the app name a section; this is the tab each one is on. */
export const TAB_OF: Record<string, TabId> = { "ask-ai": "ai", "web-search": "ai", "read-aloud": "ai", ...Object.fromEntries(TABS.map((tab) => [tab.id, tab.id])) };

function TabIcon({ path }: { path: string }) {
  return (
    <svg viewBox="0 0 20 20" className="h-4.5 w-4.5 shrink-0" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d={path} />
    </svg>
  );
}

/** A list down the side on a desktop; a row of pills that scrolls sideways on a phone. */
export function SettingsNav({ tab, onChange }: { tab: TabId; onChange: (tab: TabId) => void }) {
  const list = useRef<HTMLUListElement>(null);
  // On a phone the row scrolls sideways: keep the section in use in sight.
  useEffect(() => {
    list.current?.querySelector("[aria-current=page]")?.scrollIntoView({ block: "nearest", inline: "center" });
  }, [tab]);
  return (
    <nav aria-label="Settings sections" className="sticky top-[3.75rem] z-10 -mx-4 mb-4 border-b border-zinc-200 bg-zinc-50/95 px-4 py-2 backdrop-blur md:top-20 md:mx-0 md:mb-0 md:self-start md:border-0 md:bg-transparent md:p-0 dark:border-zinc-800 dark:bg-zinc-950/95 md:dark:bg-transparent">
      <ul ref={list} className="flex gap-1.5 overflow-x-auto [scrollbar-width:none] md:flex-col md:gap-0.5 md:overflow-visible">
        {TABS.map((item) => {
          const active = item.id === tab;
          return (
            <li key={item.id} className="shrink-0">
              <button
                type="button"
                onClick={() => onChange(item.id)}
                aria-current={active ? "page" : undefined}
                className={`flex w-full items-center gap-2.5 rounded-full px-3.5 py-1.5 text-left text-sm font-medium whitespace-nowrap transition md:rounded-lg md:px-3 md:py-2 md:whitespace-normal ${
                  active ? "bg-brand-700 text-white md:bg-brand-50 md:text-brand-700 dark:md:bg-brand-900/50 dark:md:text-brand-100" : "bg-white text-zinc-600 ring-1 ring-zinc-200 hover:bg-zinc-100 md:bg-transparent md:ring-0 dark:bg-zinc-900 dark:text-zinc-300 dark:ring-zinc-800 dark:hover:bg-zinc-800 md:dark:bg-transparent"
                }`}
              >
                <span className="hidden md:block">
                  <TabIcon path={item.icon} />
                </span>
                <span className="min-w-0">
                  <span className="block">{item.label}</span>
                  <span className={`hidden text-xs font-normal md:block ${active ? "text-brand-700/70 dark:text-brand-100/70" : "muted"}`}>{item.about}</span>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

function Copy({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="btn shrink-0"
      onClick={() =>
        navigator.clipboard
          .writeText(text)
          .then(() => {
            setDone(true);
            window.setTimeout(() => setDone(false), 1500);
          })
          .catch(() => {})
      }
    >
      {done ? "Copied" : label}
    </button>
  );
}

function Code({ children, copy }: { children: string; copy?: boolean }) {
  return (
    <div className="mt-1.5 flex items-center gap-2">
      <code className="block min-w-0 flex-1 overflow-x-auto rounded-lg bg-zinc-100 px-3 py-2 text-xs whitespace-nowrap dark:bg-zinc-800">{children}</code>
      {copy && <Copy text={children} />}
    </div>
  );
}

interface McpKey {
  id: number;
  name: string;
  /** The start and end of the key, to tell keys apart. The key itself is not kept. */
  hint: string;
  created: number;
  last_used: number | null;
}
interface McpKeys {
  keys: McpKey[];
  locked: boolean;
  admin_token: boolean;
  created?: McpKey & { key: string };
}

/** Where LLM apps connect, and the keys that let them in. */
export function McpAccess() {
  const [data, setData] = useState<McpKeys | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [fresh, setFresh] = useState<(McpKey & { key: string }) | null>(null);
  const address = `${window.location.origin}/mcp`;
  const run = (request: Promise<McpKeys>) => {
    setProblem(null);
    return request
      .then((result) => {
        setData(result);
        if (result.created) setFresh(result.created);
      })
      .catch((reason) => setProblem(String(reason.message)));
  };
  useEffect(() => {
    run(api<McpKeys>("/api/mcp/keys"));
  }, []);
  const shown = fresh?.key ?? "YOUR_KEY";
  const header = data?.locked ? ` --header "Authorization: Bearer ${shown}"` : "";
  return (
    <>
      <section className="card divide-y divide-zinc-100 px-4 dark:divide-zinc-800">
        <div className="py-3">
          <h2 className="font-medium">MCP server</h2>
          <p className="muted text-xs">LLM apps such as Claude can search and read this library over the Model Context Protocol.</p>
        </div>
        <div className="py-3 text-sm">
          <p className="font-medium">Address</p>
          <Code copy>{address}</Code>
          <p className="mt-3 font-medium">For Claude Code</p>
          <Code copy>{`claude mcp add --transport http zim-pro ${address}${header}`}</Code>
          {data?.locked && !fresh && <p className="muted mt-1.5 text-xs">Put one of your keys in place of YOUR_KEY. A key is shown only when it is made; make a new one if you no longer have it.</p>}
        </div>
      </section>

      <section className="card divide-y divide-zinc-100 px-4 dark:divide-zinc-800">
        <div className="flex flex-wrap items-center justify-between gap-3 py-3">
          <div className="min-w-0 flex-1 basis-64">
            <h2 className="font-medium">API keys</h2>
            <p className="muted text-xs">
              {data?.locked
                ? "The MCP address answers only to a caller with one of these keys" + (data.admin_token ? ", or with the admin token." : ".")
                : "No key has been made, so anyone who can reach this server can use the MCP address. Making the first key locks it."}
            </p>
          </div>
          <span className={`badge ${data?.locked ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300" : "bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300"}`}>{data?.locked ? "Locked" : "Open"}</span>
        </div>
        <ErrorNote message={problem} />
        {fresh && (
          <div className="py-3">
            <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-3.5 dark:border-emerald-900 dark:bg-emerald-950/50">
              <p className="text-sm font-medium">Your new key “{fresh.name}”</p>
              <p className="muted text-xs">Copy it now. It is not kept here, only a fingerprint of it, so it cannot be shown again.</p>
              <div className="mt-2 flex items-center gap-2">
                <code className="block min-w-0 flex-1 overflow-x-auto rounded-lg bg-white px-3 py-2 font-mono text-xs whitespace-nowrap ring-1 ring-emerald-200 dark:bg-zinc-900 dark:ring-emerald-900" aria-label="New API key">
                  {fresh.key}
                </code>
                <Copy text={fresh.key} />
              </div>
              <button type="button" className="text-brand-700 dark:text-brand-400 mt-2 text-xs font-medium hover:underline" onClick={() => setFresh(null)}>
                I have copied it
              </button>
            </div>
          </div>
        )}
        <form
          className="flex flex-wrap items-center gap-2 py-3"
          onSubmit={(event) => {
            event.preventDefault();
            run(post<McpKeys>("/api/mcp/keys", { name })).then(() => setName(""));
          }}
        >
          <input className="input min-w-0 flex-1 basis-56" placeholder="What the key is for, such as “Claude on my laptop”" value={name} maxLength={80} onChange={(e) => setName(e.target.value)} aria-label="Name for the new key" />
          <button className="btn btn-primary">Generate key</button>
        </form>
        {data && data.keys.length > 0 && (
          <ul className="divide-y divide-zinc-100 dark:divide-zinc-800">
            {data.keys.map((key) => (
              <li key={key.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 py-2.5">
                <div className="min-w-0 flex-1 basis-56">
                  <div className="truncate text-sm font-medium">{key.name}</div>
                  <div className="muted text-xs">
                    <span className="font-mono">{key.hint}</span> · made {ago(key.created)} · {key.last_used ? `last used ${ago(key.last_used)}` : "never used"}
                  </div>
                </div>
                <button
                  type="button"
                  className="btn btn-danger"
                  onClick={() => window.confirm(`Revoke “${key.name}”? Any app using it stops working at once.${data.keys.length === 1 && !data.admin_token ? " With no key left, the MCP address is open to anyone who can reach this server." : ""}`) && run(api<McpKeys>(`/api/mcp/keys/${key.id}`, { method: "DELETE" }))}
                >
                  Revoke
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}

interface AboutData {
  name: string;
  version: string;
  python: string;
  libzim: string;
  system: string;
  packaged: boolean;
  started: number;
  data_dir: string;
  state_dir: string;
  books: number;
  library_bytes: number;
  repository: string;
}
interface Latest {
  latest: string;
  url: string;
  newer: boolean;
}

function Fact({ label, children, mono = false }: { label: string; children: ReactNode; mono?: boolean }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-0.5 py-2.5">
      <dt className="muted text-sm">{label}</dt>
      <dd className={`min-w-0 text-sm break-all ${mono ? "font-mono text-xs" : ""}`}>{children}</dd>
    </div>
  );
}

const MAKER = { name: "Magicstack Enterprise LLP", site: "https://magicstack.dev" };

/** What this is, who makes it, which version runs here, and where to go for more. */
export function About() {
  const { data } = useData<AboutData>("/api/about");
  const [latest, setLatest] = useState<Latest | null>(null);
  const [checking, setChecking] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const check = () => {
    setChecking(true);
    setProblem(null);
    api<Latest>("/api/about/latest")
      .then(setLatest)
      .catch((reason) => setProblem(String(reason.message)))
      .finally(() => setChecking(false));
  };
  const repository = data?.repository ?? "https://github.com/magicstack-llp/zim-pro";
  const links = [
    { label: "Website", href: MAKER.site },
    { label: "Source code", href: repository },
    { label: "What's new", href: `${repository}/blob/master/CHANGELOG.md` },
    { label: "Releases", href: `${repository}/releases` },
    { label: "Report a problem", href: `${repository}/issues` },
    { label: "PyPI", href: "https://pypi.org/project/zim-pro/" },
  ];
  return (
    <>
      <section className="card overflow-hidden">
        <div className="from-brand-700 to-brand-500 flex flex-wrap items-center gap-4 bg-gradient-to-br px-5 py-6 text-white">
          <img src="/icon-192.png" alt="" className="h-16 w-16 shrink-0 rounded-2xl bg-white p-1 shadow-lg" />
          <div className="min-w-0 flex-1 basis-56">
            <div className="flex flex-wrap items-baseline gap-x-3">
              <h2 className="text-2xl font-bold tracking-tight">Zim Pro</h2>
              {data && <span className="rounded-full bg-white/20 px-2.5 py-0.5 text-sm font-medium">{data.version === "dev" ? "development build" : `v${data.version}`}</span>}
            </div>
            <p className="mt-1 text-sm text-white/90">Your offline knowledge library: read, search and ask your ZIM files, make your own, and serve them to LLM apps.</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3 px-5 py-4">
          <p className="text-sm">
            Made by{" "}
            <a className="text-brand-700 dark:text-brand-400 font-medium hover:underline" href={MAKER.site} target="_blank" rel="noreferrer">
              {MAKER.name}
            </a>
          </p>
          <div className="flex flex-wrap items-center gap-3">
            {latest && (
              <span className="text-sm">
                {latest.newer ? (
                  <a className="text-brand-700 dark:text-brand-400 font-medium hover:underline" href={latest.url} target="_blank" rel="noreferrer">
                    Version {latest.latest} is out
                  </a>
                ) : (
                  <span className="muted">{data?.version === "dev" ? `The newest release is ${latest.latest}` : "You have the newest version"}</span>
                )}
              </span>
            )}
            <button type="button" className="btn" onClick={check} disabled={checking}>
              {checking ? "Checking…" : "Check for a new version"}
            </button>
          </div>
        </div>
        {problem && <p className="px-5 pb-4 text-xs text-red-600 dark:text-red-400">{problem}</p>}
      </section>

      {data && (
        <section className="card px-4">
          <h2 className="py-3 font-medium">This server</h2>
          <dl className="divide-y divide-zinc-100 border-t border-zinc-100 dark:divide-zinc-800 dark:border-zinc-800">
            <Fact label="Version">{data.version === "dev" ? "Development build" : data.version}</Fact>
            <Fact label="Running since">{ago(data.started)}</Fact>
            <Fact label="Library">
              {count(data.books)} {data.books === 1 ? "book" : "books"} · {bytes(data.library_bytes)}
            </Fact>
            <Fact label="Library folder" mono>
              {data.data_dir}
            </Fact>
            <Fact label="Settings and state folder" mono>
              {data.state_dir}
            </Fact>
            <Fact label="Runs on">
              {data.system} · Python {data.python}
              {data.libzim ? ` · libzim ${data.libzim}` : ""}
              {data.packaged ? " · standalone executable" : ""}
            </Fact>
          </dl>
        </section>
      )}

      <section className="card px-4 py-3">
        <h2 className="font-medium">Links</h2>
        <ul className="mt-2 flex flex-wrap gap-2">
          {links.map((link) => (
            <li key={link.label}>
              <a className="btn" href={link.href} target="_blank" rel="noreferrer">
                {link.label}
              </a>
            </li>
          ))}
        </ul>
        <p className="muted mt-3 text-xs">
          Zim Pro reads the ZIM format through openZIM's libzim and gets its catalog of books from Kiwix. It is an independent project and is not affiliated with Kiwix or openZIM. The books belong to their own publishers, under their own licences.
        </p>
      </section>
    </>
  );
}
