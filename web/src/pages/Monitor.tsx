import { useState, type ReactNode } from "react";
import { Dot, seconds, Sparkline, usePoll, type Tone } from "../Charts";
import { bytes, count } from "../lib/format";
import { ErrorNote, Progress } from "../ui";

interface Point {
  t: number;
  cpu: number;
  system_cpu: number;
  rss: number;
  memory: number;
  threads: number;
  lag_ms: number;
  requests: number;
  request_errors: number;
  fetches: number;
  fetch_errors: number;
  fetch_bytes: number;
  download_speed: number;
  running: number;
  waiting: number;
  errors: number;
  warnings: number;
}
interface Route {
  count: number;
  errors: number;
  average_ms: number;
  p50_ms: number;
  p95_ms: number;
  slowest_ms: number;
}
interface Check {
  name: string;
  status: "ok" | "warn" | "fail" | "off";
  detail: string;
}
interface MonitorData {
  now: number;
  version: string;
  python: string;
  platform: string;
  pid: number;
  started: number;
  process: { cpu: number; rss: number; threads: number; files: number | null; lag_ms: number; listeners: number };
  system: { cpus: number; cpu: number; memory_total: number; memory_used: number; memory: number; load: number[]; booted: number };
  disks: { label: string; path: string; total: number; used: number; free: number }[];
  database: { size: number; books: number };
  metrics: {
    requests: Record<string, Route>;
    fetches: Route & { bytes: number };
    hosts: { host: string; count: number; errors: number; bytes: number; average_ms: number; last: number; last_error: string }[];
    ai: Record<string, Route>;
  };
  logs: { levels: Record<string, number>; areas: Record<string, number> };
  checks: Check[];
  series: Point[];
  step: number;
}

function Stat({ label, value, sub, values, tone, max }: { label: string; value: ReactNode; sub?: ReactNode; values: number[]; tone?: Tone; max?: number }) {
  return (
    <div className="card min-w-0 p-3">
      <div className="muted text-xs font-medium">{label}</div>
      <div className="mt-0.5 flex items-baseline gap-2">
        <span className="text-lg font-semibold tabular-nums">{value}</span>
        {sub && <span className="muted truncate text-xs">{sub}</span>}
      </div>
      <div className="mt-2">
        <Sparkline values={values} tone={tone} max={max} height={36} />
      </div>
    </div>
  );
}

function Table({ head, rows, empty }: { head: string[]; rows: ReactNode[][]; empty: string }) {
  if (!rows.length) return <p className="muted px-4 py-4 text-sm">{empty}</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="muted border-b border-zinc-100 text-left text-xs dark:border-zinc-800">
            {head.map((title, index) => (
              <th key={title} className={`px-4 py-2 font-medium whitespace-nowrap ${index ? "text-right" : ""}`}>
                {title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((cells, index) => (
            <tr key={index} className="border-b border-zinc-100 last:border-0 dark:border-zinc-800">
              {cells.map((cell, cellIndex) => (
                <td key={cellIndex} className={`px-4 py-1.5 ${cellIndex ? "text-right whitespace-nowrap tabular-nums" : "max-w-[16rem] truncate"}`}>
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const rate = (value: number, step: number) => Math.round((value / step) * 60);

/** Health checks, resources and traffic, sampled by the server every ten seconds. */
export default function Monitor() {
  const [span, setSpan] = useState<"1h" | "24h">("1h");
  const { data, error } = usePoll<MonitorData>(`/api/monitor?span=${span}`, 10_000);
  if (!data) return <ErrorNote message={error} />;
  const series = data.series;
  const pick = (key: keyof Point) => series.map((point) => point[key] as number);
  const last = series[series.length - 1];
  const step = data.step;
  const failing = data.checks.filter((check) => check.status === "fail").length;
  const warning = data.checks.filter((check) => check.status === "warn").length;
  const uptime = data.now - data.started;
  const logged = data.logs.levels;

  return (
    <>
      <ErrorNote message={error} />
      <section className="card px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 font-medium">
              <Dot status={failing ? "fail" : warning ? "warn" : "ok"} />
              {failing ? `${failing} check${failing > 1 ? "s" : ""} failing` : warning ? `${warning} to look at` : "Everything is working"}
            </h2>
            <p className="muted text-sm">
              Zim Pro {data.version} · up {seconds(uptime)} · Python {data.python} · {data.platform}
            </p>
          </div>
          <div className="flex rounded-lg border border-zinc-300 p-0.5 text-sm dark:border-zinc-700" role="group" aria-label="Time span">
            {(["1h", "24h"] as const).map((value) => (
              <button key={value} type="button" onClick={() => setSpan(value)} aria-pressed={span === value} className={`rounded-md px-3 py-1 font-medium ${span === value ? "bg-brand-700 text-white" : "muted"}`}>
                {value === "1h" ? "Last hour" : "Last day"}
              </button>
            ))}
          </div>
        </div>
        <ul className="mt-3 grid gap-x-6 gap-y-2 sm:grid-cols-2">
          {data.checks.map((check) => (
            <li key={check.name} className="flex min-w-0 items-start gap-2.5 text-sm">
              <span className="pt-1.5">
                <Dot status={check.status} />
              </span>
              <span className="min-w-0">
                <span className="font-medium">{check.name}</span>
                <span className="muted block break-words text-xs">{check.detail}</span>
              </span>
            </li>
          ))}
        </ul>
      </section>

      {series.length < 2 && <p className="muted text-sm">The charts fill in as samples are taken, one every ten seconds.</p>}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
        <Stat label="Zim Pro's CPU" value={`${last?.cpu ?? data.process.cpu}%`} sub={`machine ${last?.system_cpu ?? data.system.cpu}% of ${data.system.cpus} cores`} values={pick("cpu")} />
        <Stat label="Zim Pro's memory" value={bytes(data.process.rss)} sub={`machine ${data.system.memory.toFixed(0)}% used`} values={pick("rss")} />
        <Stat
          label="Responsiveness"
          value={`${(last?.lag_ms ?? data.process.lag_ms).toFixed(0)} ms`}
          sub="how late the server answers"
          values={pick("lag_ms")}
          tone={(last?.lag_ms ?? 0) > 500 ? "amber" : "brand"}
          max={100}
        />
        <Stat
          label="Requests to Zim Pro"
          value={`${rate(last?.requests ?? 0, step)}/min`}
          sub={last?.request_errors ? `${last.request_errors} failed` : "none failed"}
          values={pick("requests")}
          tone={last?.request_errors ? "red" : "brand"}
        />
        <Stat
          label="Pages and files fetched"
          value={`${rate(last?.fetches ?? 0, step)}/min`}
          sub={`${bytes((last?.fetch_bytes ?? 0) / step)}/s${last?.fetch_errors ? ` · ${last.fetch_errors} failed` : ""}`}
          values={pick("fetches")}
          tone={last && last.fetch_errors > last.fetches / 2 ? "amber" : "brand"}
        />
        <Stat label="Download speed" value={`${bytes(last?.download_speed ?? 0)}/s`} sub="ZIMs from the catalog" values={pick("download_speed")} />
        <Stat label="Jobs" value={`${last?.running ?? 0} running`} sub={`${last?.waiting ?? 0} waiting`} values={pick("running")} />
        <Stat
          label="Errors logged"
          value={count(logged.ERROR ?? 0)}
          sub={`${count(logged.WARNING ?? 0)} warnings in the last hour`}
          values={pick("errors")}
          tone={logged.ERROR ? "red" : "zinc"}
        />
        <Stat label="Threads" value={data.process.threads} sub={data.process.files != null ? `${data.process.files} open files` : undefined} values={pick("threads")} tone="zinc" />
      </div>

      <section className="card px-4 py-3">
        <h2 className="font-medium">Machine and storage</h2>
        <dl className="mt-2 grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
          <div className="flex justify-between gap-3">
            <dt className="muted">Memory</dt>
            <dd className="tabular-nums">
              {bytes(data.system.memory_used)} of {bytes(data.system.memory_total)}
            </dd>
          </div>
          {data.system.load.length > 0 && (
            <div className="flex justify-between gap-3">
              <dt className="muted">Load (1, 5, 15 min)</dt>
              <dd className="tabular-nums">{data.system.load.join(" · ")}</dd>
            </div>
          )}
          <div className="flex justify-between gap-3">
            <dt className="muted">Database</dt>
            <dd className="tabular-nums">
              {bytes(data.database.size)} · {count(data.database.books)} books
            </dd>
          </div>
          <div className="flex justify-between gap-3">
            <dt className="muted">Pages open live</dt>
            <dd className="tabular-nums">{data.process.listeners}</dd>
          </div>
        </dl>
        <div className="mt-3 space-y-3">
          {data.disks.map((disk) => (
            <div key={disk.label}>
              <div className="flex justify-between gap-3 text-sm">
                <span className="min-w-0 truncate">
                  {disk.label} <span className="muted text-xs">{disk.path}</span>
                </span>
                <span className="shrink-0 tabular-nums">
                  {bytes(disk.free)} free of {bytes(disk.total)}
                </span>
              </div>
              <div className="mt-1">
                <Progress value={disk.used / disk.total} tone={disk.free < 5 * 1024 ** 3 ? "red" : "brand"} />
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="card">
        <h2 className="px-4 pt-3 font-medium">Requests to Zim Pro</h2>
        <p className="muted px-4 pb-2 text-xs">Since the server started, by part of the app.</p>
        <Table
          head={["Path", "Requests", "Failed", "Average", "95% under", "Slowest"]}
          empty="No requests yet."
          rows={Object.entries(data.metrics.requests)
            .sort((a, b) => b[1].count - a[1].count)
            .map(([group, route]) => [group, count(route.count), route.errors || "", `${route.average_ms} ms`, `${route.p95_ms} ms`, `${route.slowest_ms} ms`])}
        />
      </section>

      <section className="card">
        <h2 className="px-4 pt-3 font-medium">Sites fetched from</h2>
        <p className="muted px-4 pb-2 text-xs">
          Builds and feed syncs, since the server started: {count(data.metrics.fetches.count)} fetches, {count(data.metrics.fetches.errors)} failed, {bytes(data.metrics.fetches.bytes)}. Times include waiting for a turn.
        </p>
        <Table
          head={["Site", "Fetches", "Failed", "Data", "Average", "Last problem"]}
          empty="Nothing fetched yet."
          rows={data.metrics.hosts.map((host) => [
            host.host,
            count(host.count),
            host.errors ? <span className="text-red-600 dark:text-red-400">{count(host.errors)}</span> : "",
            bytes(host.bytes),
            seconds(host.average_ms / 1000),
            <span className="muted block max-w-[14rem] truncate text-xs" title={host.last_error}>
              {host.last_error}
            </span>,
          ])}
        />
      </section>

      <section className="card">
        <h2 className="px-4 pt-3 pb-2 font-medium">AI providers</h2>
        <Table
          head={["Provider", "Answers", "Failed", "Average", "95% under", "Slowest"]}
          empty="No questions asked since the server started."
          rows={Object.entries(data.metrics.ai).map(([name, route]) => [
            name,
            count(route.count),
            route.errors || "",
            seconds(route.average_ms / 1000),
            seconds(route.p95_ms / 1000),
            seconds(route.slowest_ms / 1000),
          ])}
        />
      </section>
    </>
  );
}
