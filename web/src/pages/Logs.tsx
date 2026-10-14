import { useCallback, useEffect, useRef, useState } from "react";
import { api, authHeaders, post } from "../lib/api";
import { ErrorNote, Switch } from "../ui";

interface Line {
  id: number;
  time: number;
  level: "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";
  area: string;
  logger: string;
  job: string;
  message: string;
}
interface LogPage {
  lines: Line[];
  newest: number;
  areas: string[];
  jobs: string[];
  level: string;
  file: string;
  redis: boolean;
}

const LEVEL_STYLE: Record<string, string> = {
  DEBUG: "bg-zinc-100 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400",
  INFO: "bg-sky-50 text-sky-700 dark:bg-sky-950 dark:text-sky-300",
  WARNING: "bg-amber-50 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  ERROR: "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300",
  CRITICAL: "bg-red-600 text-white",
};
const SHOW = [
  { value: "", label: "Everything kept" },
  { value: "INFO", label: "Info and above" },
  { value: "WARNING", label: "Warnings and errors" },
  { value: "ERROR", label: "Errors only" },
];
const KEEP = [
  { value: "DEBUG", label: "Detail (every web request)" },
  { value: "INFO", label: "Info" },
  { value: "WARNING", label: "Warnings and errors" },
  { value: "ERROR", label: "Errors only" },
];
const PAGE = 300;

function clock(time: number): string {
  return new Date(time * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function Row({ line, onJob }: { line: Line; onJob: (job: string) => void }) {
  const [open, setOpen] = useState(false);
  const [first, ...rest] = line.message.split("\n");
  return (
    <li className="border-b border-zinc-100 px-3 py-1.5 last:border-0 dark:border-zinc-800">
      <div className="flex min-w-0 items-start gap-2 text-xs">
        <time className="muted shrink-0 pt-0.5 font-mono tabular-nums" title={new Date(line.time * 1000).toLocaleString()} dateTime={new Date(line.time * 1000).toISOString()}>
          {clock(line.time)}
        </time>
        <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-wide ${LEVEL_STYLE[line.level] ?? LEVEL_STYLE.INFO}`}>{line.level === "WARNING" ? "WARN" : line.level}</span>
        <span className="badge shrink-0 !py-0 text-[11px]" title={line.logger}>
          {line.area}
        </span>
        {line.job && (
          <button type="button" className="shrink-0 rounded bg-brand-50 px-1.5 py-0.5 text-[11px] font-medium text-brand-700 hover:bg-brand-100 dark:bg-brand-900/50 dark:text-brand-100" onClick={() => onJob(line.job)} title="Show only this job">
            {line.job}
          </button>
        )}
        <button type="button" disabled={!rest.length} onClick={() => setOpen(!open)} className="min-w-0 flex-1 text-left font-mono break-words whitespace-pre-wrap disabled:cursor-text">
          {first}
          {rest.length > 0 && !open && <span className="muted"> … ({rest.length} more lines)</span>}
          {open && rest.length > 0 && <span className="muted block">{rest.join("\n")}</span>}
        </button>
      </div>
    </li>
  );
}

/** Every line the server logged, with filters, live updates and the files to download. */
export default function Logs({ onKeepLevel }: { onKeepLevel: (level: string) => Promise<void> }) {
  const [lines, setLines] = useState<Line[]>([]);
  const [info, setInfo] = useState<Omit<LogPage, "lines"> | null>(null);
  const [level, setLevel] = useState("");
  const [area, setArea] = useState("");
  const [job, setJob] = useState("");
  const [text, setText] = useState("");
  const [query, setQuery] = useState("");
  const [live, setLive] = useState(true);
  const [more, setMore] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const newest = useRef(0);

  const params = useCallback(
    (extra: Record<string, string | number>) => {
      const search = new URLSearchParams({ level, area, job, q: query, limit: String(PAGE) });
      for (const [key, value] of Object.entries(extra)) search.set(key, String(value));
      return `/api/logs?${search}`;
    },
    [level, area, job, query],
  );

  // A new set of filters: start again from the newest lines.
  useEffect(() => {
    let gone = false;
    api<LogPage>(params({}))
      .then((page) => {
        if (gone) return;
        const { lines: got, ...rest } = page;
        setLines(got);
        setInfo(rest);
        setMore(got.length >= PAGE);
        newest.current = page.newest;
        setProblem(null);
      })
      .catch((reason) => !gone && setProblem(String(reason.message ?? reason)));
    return () => {
      gone = true;
    };
  }, [params]);

  // Live: every two seconds, what was logged since.
  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "visible" || !newest.current) return;
      api<LogPage>(params({ after: newest.current }))
        .then((page) => {
          const { lines: got, ...rest } = page;
          newest.current = page.newest;
          setInfo(rest);
          if (got.length) setLines((current) => [...got, ...current].slice(0, 5000));
        })
        .catch(() => {});
    }, 2000);
    return () => window.clearInterval(timer);
  }, [live, params]);

  // Typing settles before the server is asked.
  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(text.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [text]);

  const older = async () => {
    const oldest = lines[lines.length - 1]?.id;
    if (!oldest) return;
    const page = await api<LogPage>(params({ before: oldest }));
    setLines((current) => [...current, ...page.lines]);
    setMore(page.lines.length >= PAGE);
  };
  const download = async () => {
    setProblem(null);
    try {
      const response = await fetch("/api/logs/download", { headers: authHeaders() });
      if (!response.ok) throw new Error(response.statusText);
      const link = document.createElement("a");
      link.href = URL.createObjectURL(await response.blob());
      link.download = /filename="([^"]+)"/.exec(response.headers.get("content-disposition") ?? "")?.[1] ?? "zim-pro-logs.jsonl";
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (reason) {
      setProblem(`The logs could not be downloaded: ${(reason as Error).message}`);
    }
  };
  const clear = async () => {
    if (!window.confirm("Empty this list? The log files keep every line.")) return;
    await post("/api/logs/clear");
    setLines([]);
  };
  const keep = async (value: string) => {
    setProblem(null);
    try {
      await onKeepLevel(value);
      setInfo((current) => current && { ...current, level: value });
    } catch (reason) {
      setProblem(String((reason as Error).message));
    }
  };
  const filtered = level || area || job || query;

  return (
    <>
      <ErrorNote message={problem} />
      <section className="card px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <h2 className="font-medium">Logs</h2>
            <p className="muted text-sm">
              Everything the server did, from every part of it. Lines carry the job they were written for: click one to follow it.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button type="button" className="btn" onClick={download}>
              Download
            </button>
            <button type="button" className="btn btn-danger" onClick={clear}>
              Clear list
            </button>
          </div>
        </div>
        <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          <input className="input" type="search" placeholder="Search the messages" value={text} onChange={(e) => setText(e.target.value)} aria-label="Search the messages" />
          <select className="input" value={level} onChange={(e) => setLevel(e.target.value)} aria-label="Show">
            {SHOW.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <select className="input" value={area} onChange={(e) => setArea(e.target.value)} aria-label="Part of Zim Pro">
            <option value="">Every part</option>
            {(info?.areas ?? []).map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <select className="input" value={job} onChange={(e) => setJob(e.target.value)} aria-label="Job">
            <option value="">Every job</option>
            {[...new Set([job, ...(info?.jobs ?? [])].filter(Boolean))].map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2 text-sm">
          <label className="flex items-center gap-2">
            <Switch checked={live} onChange={setLive} label="Live" small />
            Live
          </label>
          <label className="flex items-center gap-2">
            <span className="muted">Keep</span>
            <select className="input !w-auto !py-1" value={info?.level ?? "INFO"} onChange={(e) => keep(e.target.value)} aria-label="Lines to keep">
              {KEEP.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          {filtered && (
            <button
              type="button"
              className="text-brand-700 dark:text-brand-400 font-medium"
              onClick={() => {
                setLevel("");
                setArea("");
                setJob("");
                setText("");
              }}
            >
              Show all
            </button>
          )}
        </div>
        {info && (
          <p className="muted mt-2 text-xs break-all">
            {info.file ? <>Written to {info.file} (5 files of 5 MB are kept)</> : "Kept in memory only"}
            {info.redis && <> and to the Redis stream zimpro:logs</>}.
          </p>
        )}
      </section>
      <section className="card overflow-hidden">
        {lines.length === 0 ? (
          <p className="muted px-4 py-10 text-center text-sm">{filtered ? "No line matches." : "Nothing logged yet."}</p>
        ) : (
          <ul className="max-h-[70vh] overflow-y-auto">
            {lines.map((line) => (
              <Row key={line.id} line={line} onJob={setJob} />
            ))}
          </ul>
        )}
        {more && (
          <div className="border-t border-zinc-100 p-2 text-center dark:border-zinc-800">
            <button type="button" className="btn" onClick={older}>
              Older lines
            </button>
          </div>
        )}
      </section>
    </>
  );
}
