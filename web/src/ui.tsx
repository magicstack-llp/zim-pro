import { useState, type ReactNode } from "react";

export function BookIcon({ src, title, size = 40 }: { src?: string | null; title: string; size?: number }) {
  const [failed, setFailed] = useState(false);
  const style = { width: size, height: size };
  if (!src || failed) {
    return (
      <div
        style={style}
        className="bg-brand-100 text-brand-700 dark:bg-brand-900 dark:text-brand-100 flex shrink-0 items-center justify-center rounded-lg text-sm font-semibold"
        aria-hidden
      >
        {title.trim().charAt(0).toUpperCase() || "?"}
      </div>
    );
  }
  return (
    <img
      src={src}
      alt=""
      loading="lazy"
      style={style}
      onError={() => setFailed(true)}
      className="shrink-0 rounded-lg bg-white object-contain p-0.5"
    />
  );
}

export function Switch({ checked, onChange, label, small = false, disabled = false }: { checked: boolean; onChange: (value: boolean) => void; label: string; small?: boolean; disabled?: boolean }) {
  const [track, knob, travel] = small ? ["h-[18px] w-8", "h-3.5 w-3.5", "translate-x-3.5"] : ["h-6 w-11", "h-5 w-5", "translate-x-5"];
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative ${track} shrink-0 rounded-full transition disabled:opacity-50 ${checked ? "bg-brand-600" : "bg-zinc-300 dark:bg-zinc-700"}`}
    >
      <span className={`absolute top-0.5 left-0.5 ${knob} rounded-full bg-white shadow transition-transform ${checked ? travel : ""}`} />
    </button>
  );
}

export function Progress({ value, tone = "brand" }: { value: number; tone?: "brand" | "muted" | "red" }) {
  const color = { brand: "bg-brand-500", muted: "bg-zinc-400", red: "bg-red-500" }[tone];
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800">
      <div className={`h-full ${color} transition-[width] duration-500`} style={{ width: `${Math.min(100, value * 100)}%` }} />
    </div>
  );
}

export function PageHeader({ title, subtitle, children }: { title: string; subtitle?: ReactNode; children?: ReactNode }) {
  return (
    <div className="mb-4 flex flex-wrap items-end justify-between gap-3 sm:mb-5">
      <div className="min-w-0">
        <h1 className="text-[1.7rem] leading-tight font-bold tracking-tight sm:text-2xl sm:font-semibold">{title}</h1>
        {subtitle && <p className="muted mt-1 text-sm">{subtitle}</p>}
      </div>
      {children && <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto">{children}</div>}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="card px-6 py-12 text-center">
      <p className="font-medium">{title}</p>
      {children && <div className="muted mx-auto mt-1 max-w-md text-sm">{children}</div>}
    </div>
  );
}

export function ErrorNote({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-2.5 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
      {message}
    </div>
  );
}

export function Tabs<T extends string>({
  value,
  onChange,
  options,
}: {
  value: T;
  onChange: (value: T) => void;
  options: { value: T; label: string; count?: number }[];
}) {
  return (
    <div className="mb-4 flex gap-1 overflow-x-auto overflow-y-hidden border-b border-zinc-200 [scrollbar-width:none] dark:border-zinc-800">
      {options.map((option) => (
        <button
          key={option.value}
          onClick={() => onChange(option.value)}
          className={`border-b-2 px-3 py-2.5 text-sm font-medium whitespace-nowrap sm:py-2 ${
            value === option.value
              ? "border-brand-600 text-brand-700 dark:text-brand-400"
              : "muted border-transparent hover:text-zinc-800 dark:hover:text-zinc-200"
          }`}
        >
          {option.label}
          {option.count !== undefined && <span className="badge ml-2">{option.count}</span>}
        </button>
      ))}
    </div>
  );
}
