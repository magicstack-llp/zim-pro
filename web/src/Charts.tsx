import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./lib/api";

/** Fetch JSON now and every `ms`, while the page is in view. */
export function usePoll<T>(path: string, ms: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const latest = useRef(0);
  const load = useCallback(() => {
    const request = ++latest.current;
    api<T>(path)
      .then((result) => {
        if (request !== latest.current) return;
        setData(result);
        setError(null);
      })
      .catch((reason) => request === latest.current && setError(String(reason.message ?? reason)));
  }, [path]);
  useEffect(() => {
    load();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, ms);
    return () => window.clearInterval(timer);
  }, [load, ms]);
  return { data, error, reload: load, setData };
}

const TONES = {
  brand: ["stroke-brand-600 dark:stroke-brand-400", "fill-brand-500/15"],
  amber: ["stroke-amber-500", "fill-amber-500/15"],
  red: ["stroke-red-500", "fill-red-500/15"],
  zinc: ["stroke-zinc-400", "fill-zinc-400/15"],
} as const;
export type Tone = keyof typeof TONES;

/** A line over time, filled below; stretches to the width it is given. */
export function Sparkline({ values, tone = "brand", height = 40, max }: { values: number[]; tone?: Tone; height?: number; max?: number }) {
  if (values.length < 2) return <div style={{ height }} className="rounded bg-zinc-100 dark:bg-zinc-800/60" />;
  const top = Math.max(max ?? 0, ...values) || 1;
  const width = 100;
  const step = width / (values.length - 1);
  const points = values.map((value, index) => `${(index * step).toFixed(2)},${(height - (Math.max(0, value) / top) * (height - 2) - 1).toFixed(2)}`);
  const [line, fill] = TONES[tone];
  return (
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className="block w-full" style={{ height }} aria-hidden>
      <polygon points={`0,${height} ${points.join(" ")} ${width},${height}`} className={fill} />
      <polyline points={points.join(" ")} fill="none" className={line} strokeWidth={1.5} vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
    </svg>
  );
}

/** Columns side by side, each a stack of parts (finished in brand, failed in red, …). */
export function Bars({ columns, height = 48, labels }: { columns: { parts: { value: number; tone: Tone }[]; title: string }[]; height?: number; labels?: [string, string] }) {
  const top = Math.max(1, ...columns.map((column) => column.parts.reduce((sum, part) => sum + part.value, 0)));
  const fills: Record<Tone, string> = { brand: "bg-brand-500", amber: "bg-amber-400", red: "bg-red-500", zinc: "bg-zinc-400" };
  return (
    <div>
      <div className="flex items-end gap-px" style={{ height }}>
        {columns.map((column, index) => (
          <div key={index} title={column.title} className="flex h-full min-w-0 flex-1 flex-col-reverse rounded-sm bg-zinc-100 dark:bg-zinc-800/60">
            {column.parts.map((part, partIndex) =>
              part.value > 0 ? <div key={partIndex} className={`${fills[part.tone]} first:rounded-b-sm last:rounded-t-sm`} style={{ height: `${(part.value / top) * 100}%` }} /> : null,
            )}
          </div>
        ))}
      </div>
      {labels && (
        <div className="muted mt-1 flex justify-between text-[11px]">
          <span>{labels[0]}</span>
          <span>{labels[1]}</span>
        </div>
      )}
    </div>
  );
}

/** A coloured dot for a state: working, worth a look, failing, or not in use. */
export function Dot({ status }: { status: "ok" | "warn" | "fail" | "off" }) {
  const color = { ok: "bg-emerald-500 shadow-[0_0_6px] shadow-emerald-500/60", warn: "bg-amber-400 shadow-[0_0_6px] shadow-amber-400/60", fail: "bg-red-500 shadow-[0_0_6px] shadow-red-500/60", off: "bg-zinc-300 dark:bg-zinc-600" }[status];
  return <span className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full ${color}`} aria-label={status} />;
}

export function seconds(value: number | null | undefined): string {
  if (value == null || !isFinite(value)) return "—";
  if (value < 1) return `${Math.round(value * 1000)} ms`;
  if (value < 90) return `${value.toFixed(value < 10 ? 1 : 0)} s`;
  if (value < 5400) return `${Math.round(value / 60)} min`;
  if (value < 172800) return `${(value / 3600).toFixed(1)} h`;
  return `${Math.round(value / 86400)} days`;
}
