import { useState, type ReactNode } from "react";
import { Bars, Dot, seconds, usePoll } from "../Charts";
import { api } from "../lib/api";
import { ago, bytes, count } from "../lib/format";
import { ErrorNote, Progress } from "../ui";

interface Running {
  id: number;
  title: string;
  state: string;
  phase: string;
  detail?: string;
  done: number;
  total: number;
  failed?: number;
  speed?: number;
  unit: "bytes" | "items";
  running_for: number | null;
  quiet_for: number | null;
  stalled: boolean;
}
interface Queue {
  name: "downloads" | "builds" | "feeds";
  label: string;
  about: string;
  paused: boolean;
  concurrency: number;
  concurrency_max: number;
  counts: Record<"waiting" | "running" | "paused" | "finished" | "failed" | "cancelled", number>;
  day: { finished: number; failed: number; cancelled: number; average_wait: number | null; average_run: number | null; p95_run: number | null };
  oldest_wait: number | null;
  running: Running[];
  waiting: { id: number; title: string; place: number; waited: number | null }[];
  paused_jobs: { id: number; title: string; created: number }[];
  hours: { t: number; finished: number; failed: number; cancelled: number }[];
  failures: { id: number; title: string; error: string | null; finished: number }[];
}
interface Broker {
  kind: "database" | "redis";
  ok: boolean;
  url?: string;
  error?: string;
  version?: string;
  latency_ms?: number;
  memory?: number;
  clients?: number;
  ops_per_second?: number;
  keys?: number;
  log_lines?: number;
  uptime?: number;
  calls?: number;
  failures?: number;
}
interface QueuesData {
  broker: Broker;
  queues: Queue[];
}

function Icon({ path }: { path: string }) {
  return (
    <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d={path} />
    </svg>
  );
}
const ICONS = {
  top: "M5 4h10M10 16V8M6 11l4-4 4 4",
  up: "M10 15V5M6 9l4-4 4 4",
  down: "M10 5v10M6 11l4 4 4-4",
  bottom: "M5 16h10M10 4v8M6 9l4 4 4-4",
  pause: "M7.500 5v10M12.500 5v10",
  resume: "M7 5l8 5-8 5z",
  cancel: "M6 6l8 8M14 6l-8 8",
  retry: "M15 9a5 5 0 1 0-1.500 4.500M15 4v5h-5",
};

function IconButton({ label, icon, onClick, danger }: { label: string; icon: keyof typeof ICONS; onClick: () => void; danger?: boolean }) {
  return (
    <button type="button" title={label} aria-label={label} onClick={onClick} className={`rounded-lg p-1.5 ${danger ? "text-red-600 hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950" : "muted hover:bg-zinc-100 hover:text-zinc-900 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"}`}>
      <Icon path={ICONS[icon]} />
    </button>
  );
}

function Figure({ label, value, warn }: { label: string; value: ReactNode; warn?: boolean }) {
  return (
    <div className="min-w-0">
      <div className="muted truncate text-xs">{label}</div>
      <div className={`font-semibold tabular-nums ${warn ? "text-red-600 dark:text-red-400" : ""}`}>{value}</div>
    </div>
  );
}

function BrokerCard({ broker }: { broker: Broker }) {
  if (broker.kind === "database") {
    return (
      <section className="card px-4 py-3">
        <h2 className="flex items-center gap-2 font-medium">
          <Dot status="off" /> Queues kept in the database
        </h2>
        <p className="muted mt-1 text-sm">
          Redis is not set up. Everything works without it. To keep the queues in Redis (where other tools can watch and change them) and stream the logs to it, run a Redis server and start Zim Pro with{" "}
          <code className="rounded bg-zinc-100 px-1 dark:bg-zinc-800">KIWIX_REDIS_URL=redis://redis:6379/0</code>. The compose files in the repository include one.
        </p>
      </section>
    );
  }
  return (
    <section className="card px-4 py-3">
      <h2 className="flex items-center gap-2 font-medium">
        <Dot status={broker.ok ? "ok" : "fail"} /> Redis {broker.version}
        <span className="muted min-w-0 truncate text-sm font-normal">{broker.url}</span>
      </h2>
      {broker.ok ? (
        <div className="mt-2 grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7">
          <Figure label="Answers in" value={`${broker.latency_ms} ms`} />
          <Figure label="Memory" value={bytes(broker.memory ?? 0)} />
          <Figure label="Clients" value={broker.clients} />
          <Figure label="Commands/s" value={broker.ops_per_second} />
          <Figure label="Zim Pro keys" value={broker.keys} />
          <Figure label="Log lines" value={count(broker.log_lines ?? 0)} />
          <Figure label="Up" value={seconds(broker.uptime ?? 0)} />
        </div>
      ) : (
        <p className="mt-1 text-sm text-red-700 dark:text-red-400">
          {broker.error || "It cannot be reached"}. The queues go on from the database, and Redis is filled again when it is back.
        </p>
      )}
      <p className="muted mt-2 text-xs">
        Each queue is the sorted set <code>zimpro:queue:&lt;name&gt;</code> (lower score starts first) with a <code>…:paused</code> key; changes made there are followed within five seconds. Logs go to the stream <code>zimpro:logs</code>.
      </p>
    </section>
  );
}

function QueueCard({ queue, act, onConcurrency }: { queue: Queue; act: (path: string, method?: string, body?: unknown) => Promise<void>; onConcurrency: (value: number) => Promise<void> }) {
  const base = `/api/queues/${queue.name}`;
  const job = (id: number, action: string) => act(`${base}/jobs/${id}/${action}`);
  const hourLabel = (t: number) => new Date(t * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return (
    <section className="card">
      <div className="flex flex-wrap items-start justify-between gap-3 px-4 pt-3">
        <div className="min-w-0">
          <h2 className="flex items-center gap-2 font-medium">
            <Dot status={queue.paused ? "warn" : queue.running.some((job) => job.stalled) ? "warn" : queue.counts.running ? "ok" : "off"} />
            {queue.label}
            {queue.paused && <span className="badge !bg-amber-100 !text-amber-800 dark:!bg-amber-950 dark:!text-amber-300">Paused</span>}
          </h2>
          <p className="muted text-sm">{queue.paused ? "Nothing new starts; what is running goes on." : queue.about}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex items-center rounded-lg border border-zinc-300 dark:border-zinc-700" role="group" aria-label="Jobs at once">
            <button type="button" className="px-2.5 py-1 disabled:opacity-40" disabled={queue.concurrency <= 1} onClick={() => onConcurrency(queue.concurrency - 1)} aria-label="Fewer at once">
              −
            </button>
            <span className="px-1 text-sm tabular-nums" title="Jobs run at once">
              {queue.concurrency} at once
            </span>
            <button type="button" className="px-2.5 py-1 disabled:opacity-40" disabled={queue.concurrency >= queue.concurrency_max} onClick={() => onConcurrency(queue.concurrency + 1)} aria-label="More at once">
              +
            </button>
          </div>
          <button type="button" className={`btn ${queue.paused ? "btn-primary" : ""}`} onClick={() => act(`${base}/${queue.paused ? "resume" : "pause"}`)}>
            {queue.paused ? "Resume queue" : "Pause queue"}
          </button>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-3 px-4 py-3 sm:grid-cols-5 lg:grid-cols-9">
        <Figure label="Waiting" value={queue.counts.waiting} />
        <Figure label="Running" value={queue.counts.running} />
        <Figure label="Paused" value={queue.counts.paused} />
        <Figure label="Done, 24 h" value={queue.day.finished} />
        <Figure label="Failed, 24 h" value={queue.day.failed} warn={queue.day.failed > 0} />
        <Figure label="Average wait" value={seconds(queue.day.average_wait)} />
        <Figure label="Average run" value={seconds(queue.day.average_run)} />
        <Figure label="95% within" value={seconds(queue.day.p95_run)} />
        <Figure label="Oldest wait" value={seconds(queue.oldest_wait)} />
      </div>
      <div className="px-4 pb-3">
        <Bars
          height={40}
          labels={["24 h ago", "now"]}
          columns={queue.hours.map((hour) => ({
            title: `${hourLabel(hour.t)}: ${hour.finished} done, ${hour.failed} failed, ${hour.cancelled} cancelled`,
            parts: [
              { value: hour.finished, tone: "brand" },
              { value: hour.failed, tone: "red" },
              { value: hour.cancelled, tone: "zinc" },
            ],
          }))}
        />
      </div>

      {queue.running.length > 0 && (
        <div className="border-t border-zinc-100 dark:border-zinc-800">
          <h3 className="muted px-4 pt-2 text-xs font-semibold tracking-wide uppercase">Running</h3>
          <ul>
            {queue.running.map((item) => (
              <li key={item.id} className="px-4 py-2">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium">{item.title}</div>
                    <div className="muted text-xs">
                      #{item.id} · {item.state === "paused" ? "paused" : item.phase || item.state}
                      {item.unit === "bytes"
                        ? ` · ${bytes(item.done)} of ${bytes(item.total)}${item.speed ? ` · ${bytes(item.speed)}/s` : ""}`
                        : item.total
                          ? ` · ${count(item.done)} of ${count(item.total)}${item.failed ? `, ${item.failed} failed` : ""}`
                          : ""}
                      {item.running_for != null && ` · running ${seconds(item.running_for)}`}
                      {item.detail && ` · ${item.detail}`}
                    </div>
                    {item.stalled ? (
                      <div className="mt-0.5 text-xs font-medium text-amber-700 dark:text-amber-400">Not moving for {seconds(item.quiet_for)}: it may be stuck. Cancel it and retry if it stays so.</div>
                    ) : item.quiet_for != null && item.quiet_for > 60 ? (
                      <div className="muted mt-0.5 text-xs">Last moved {seconds(item.quiet_for)} ago</div>
                    ) : null}
                  </div>
                  <div className="flex shrink-0">
                    {queue.name !== "downloads" && (item.state === "paused" ? <IconButton label="Resume" icon="resume" onClick={() => job(item.id, "resume")} /> : <IconButton label="Pause" icon="pause" onClick={() => job(item.id, "pause")} />)}
                    {queue.name === "downloads" && <IconButton label="Pause" icon="pause" onClick={() => job(item.id, "pause")} />}
                    <IconButton label="Cancel" icon="cancel" danger onClick={() => window.confirm(`Cancel ${item.title}?`) && job(item.id, "cancel")} />
                  </div>
                </div>
                {item.total > 0 && (
                  <div className="mt-1.5">
                    <Progress value={item.done / item.total} tone={item.stalled ? "red" : "brand"} />
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {queue.waiting.length > 0 && (
        <div className="border-t border-zinc-100 dark:border-zinc-800">
          <div className="flex items-center justify-between px-4 pt-2">
            <h3 className="muted text-xs font-semibold tracking-wide uppercase">Waiting, in the order they start</h3>
            <button type="button" className="text-xs font-medium text-red-700 dark:text-red-400" onClick={() => window.confirm(`Cancel all ${queue.waiting.length} waiting?`) && act(`${base}/cancel-waiting`)}>
              Cancel all
            </button>
          </div>
          <ol>
            {queue.waiting.map((item, index) => (
              <li key={item.id} className="flex items-center gap-2 px-4 py-1.5">
                <span className="muted w-6 shrink-0 text-right text-xs tabular-nums">{index + 1}</span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm">{item.title}</span>
                  <span className="muted text-xs">waiting {seconds(item.waited)}</span>
                </span>
                <span className="flex shrink-0">
                  <IconButton label="To the top" icon="top" onClick={() => job(item.id, "top")} />
                  <IconButton label="Up one" icon="up" onClick={() => job(item.id, "up")} />
                  <IconButton label="Down one" icon="down" onClick={() => job(item.id, "down")} />
                  <span className="hidden sm:flex">
                    <IconButton label="To the bottom" icon="bottom" onClick={() => job(item.id, "bottom")} />
                    <IconButton label="Hold" icon="pause" onClick={() => job(item.id, "pause")} />
                  </span>
                  <IconButton label="Cancel" icon="cancel" danger onClick={() => job(item.id, "cancel")} />
                </span>
              </li>
            ))}
          </ol>
        </div>
      )}

      {queue.paused_jobs.length > 0 && (
        <div className="border-t border-zinc-100 dark:border-zinc-800">
          <h3 className="muted px-4 pt-2 text-xs font-semibold tracking-wide uppercase">Held</h3>
          <ul>
            {queue.paused_jobs.map((item) => (
              <li key={item.id} className="flex items-center gap-2 px-4 py-1.5">
                <span className="min-w-0 flex-1 truncate text-sm">{item.title}</span>
                <IconButton label="Resume" icon="resume" onClick={() => job(item.id, "resume")} />
                <IconButton label="Cancel" icon="cancel" danger onClick={() => job(item.id, "cancel")} />
              </li>
            ))}
          </ul>
        </div>
      )}

      {queue.failures.length > 0 && (
        <div className="border-t border-zinc-100 dark:border-zinc-800">
          <div className="flex items-center justify-between px-4 pt-2">
            <h3 className="muted text-xs font-semibold tracking-wide uppercase">Failed in the last day</h3>
            <button type="button" className="text-brand-700 dark:text-brand-400 text-xs font-medium" onClick={() => act(`${base}/retry-failed`)}>
              Retry all failed
            </button>
          </div>
          <ul>
            {queue.failures.map((item) => (
              <li key={item.id} className="flex items-start gap-2 px-4 py-1.5">
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm">{item.title}</span>
                  <span className="block text-xs break-words text-red-700 dark:text-red-400">{item.error}</span>
                  <span className="muted text-xs">{ago(item.finished)}</span>
                </span>
                <IconButton label="Retry" icon="retry" onClick={() => job(item.id, "retry")} />
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-zinc-100 px-4 py-2 text-xs dark:border-zinc-800">
        <span className="muted">
          All time: {count(queue.counts.finished)} done, {count(queue.counts.failed)} failed, {count(queue.counts.cancelled)} cancelled
        </span>
        <button
          type="button"
          className="muted font-medium hover:text-zinc-900 dark:hover:text-zinc-100"
          onClick={() => window.confirm(`Remove the finished ${queue.label.toLowerCase()} from the lists?`) && act(`${base}/clear-finished`)}
        >
          Clear finished
        </button>
      </div>
    </section>
  );
}

const SETTING_OF = { downloads: "max_concurrent_downloads", builds: "max_concurrent_builds", feeds: "max_concurrent_feed_syncs" } as const;

/** The three queues: what is running, waiting and failing, and the means to change it. */
export default function Queues({ onSetting }: { onSetting: (patch: Record<string, number>) => void }) {
  const { data, error, setData } = usePoll<QueuesData>("/api/queues", 3000);
  const [problem, setProblem] = useState<string | null>(null);

  const replace = (queue: Queue) => setData((current) => current && { ...current, queues: current.queues.map((q) => (q.name === queue.name ? queue : q)) });
  const act = async (path: string, method = "POST", body?: unknown) => {
    setProblem(null);
    try {
      const result = await api<Queue | { queue: Queue }>(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
      replace("queue" in result ? result.queue : result);
    } catch (reason) {
      setProblem(String((reason as Error).message));
    }
  };
  const concurrency = (queue: Queue) => async (value: number) => {
    await act(`/api/queues/${queue.name}/concurrency`, "PUT", { value });
    onSetting({ [SETTING_OF[queue.name]]: value });
  };
  if (!data) return <ErrorNote message={error} />;
  return (
    <>
      <ErrorNote message={problem ?? error} />
      <BrokerCard broker={data.broker} />
      {data.queues.map((queue) => (
        <QueueCard key={queue.name} queue={queue} act={act} onConcurrency={concurrency(queue)} />
      ))}
    </>
  );
}
