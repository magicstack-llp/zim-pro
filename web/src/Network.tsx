import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, post } from "./lib/api";
import { ago } from "./lib/format";
import { useLive } from "./lib/live";
import { Switch } from "./ui";

/** The VPN and the captcha solver that builds and feeds fetch through: state and switches. */

export interface Vpn {
  /** A proxy address has been set. */
  proxy_set: boolean;
  /** Requests are sent through it. */
  enabled: boolean;
  /** A Gluetun control server has been set, so the connection can be seen and changed. */
  control: boolean;
  /** Ask for another address when a site answers "too many requests". */
  change_ip: boolean;
  status: string;
  error: string;
  changing: boolean;
  changes: number;
  last_change: number | null;
  last_change_error: string;
  public_ip: string;
  country: string;
  region: string;
  city: string;
  organization: string;
  timezone: string;
}
export interface Solver {
  set: boolean;
  enabled: boolean;
  ready: boolean;
  version: string;
  error: string;
}
export interface NetworkState {
  vpn: Vpn;
  solver: Solver;
}

type Tone = "green" | "amber" | "red" | "grey";
const DOT: Record<Tone, string> = { green: "bg-emerald-500", amber: "bg-amber-500", red: "bg-red-500", grey: "bg-zinc-400 dark:bg-zinc-600" };

function Dot({ tone, pulse = false }: { tone: Tone; pulse?: boolean }) {
  return (
    <span className="relative inline-flex h-2 w-2 shrink-0" aria-hidden>
      {pulse && <span className={`absolute inline-flex h-full w-full animate-ping rounded-full opacity-70 ${DOT[tone]}`} />}
      <span className={`relative inline-flex h-2 w-2 rounded-full ${DOT[tone]}`} />
    </span>
  );
}

/** What the VPN is doing, in a few words, and whether that is fine. */
export function vpnSummary(vpn: Vpn): { tone: Tone; title: string; line: string } {
  if (!vpn.proxy_set) return { tone: "grey", title: "Not set up", line: "Add the proxy address in Settings to send requests through it." };
  if (!vpn.enabled) return { tone: "grey", title: "Off", line: "Requests leave from this machine's own address." };
  if (vpn.changing) return { tone: "amber", title: "Changing IP…", line: "Requests wait until the new connection is up." };
  if (!vpn.control) return { tone: "green", title: "On", line: "Sent through the proxy. Add the control server to see the connection." };
  if (vpn.error) return { tone: "red", title: "No answer", line: vpn.error };
  if (vpn.status === "running" && vpn.public_ip) return { tone: "green", title: "Connected", line: [vpn.country, vpn.city || vpn.region].filter(Boolean).join(" · ") };
  if (vpn.status === "running") return { tone: "amber", title: "Connecting…", line: "The VPN is up but has not said which address it has yet." };
  if (vpn.status === "stopped") return { tone: "red", title: "Stopped", line: "The VPN is stopped: pages cannot be fetched through it." };
  return { tone: "amber", title: vpn.status ? vpn.status.charAt(0).toUpperCase() + vpn.status.slice(1) : "Unknown", line: "" };
}

export function solverSummary(solver: Solver): { tone: Tone; title: string; line: string } {
  if (!solver.enabled) return { tone: "grey", title: "Off", line: "Pages behind a browser check fail with a note." };
  if (solver.error) return { tone: "red", title: "No answer", line: solver.error };
  if (solver.ready) return { tone: "green", title: "Ready", line: solver.version ? `FlareSolverr ${solver.version.replace(/^v/, "")}` : "FlareSolverr" };
  return { tone: "amber", title: "Checking…", line: "" };
}

/** The state of the VPN and the solver, kept current, with the actions on them. */
export function useNetwork() {
  const { network } = useLive();
  const [state, setState] = useState<NetworkState | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback((fresh = false) => {
    return api<NetworkState>(`/api/network${fresh ? "?fresh=true" : ""}`)
      .then(setState)
      .catch(() => {});
  }, []);
  useEffect(() => {
    load();
    const timer = window.setInterval(() => document.visibilityState === "visible" && load(), 30_000);
    return () => window.clearInterval(timer);
  }, [load, network]);
  /** Flip one of the switches; the page shows it at once and the server confirms. */
  const turn = (key: "build_proxy_enabled" | "captcha_solver_enabled" | "vpn_change_ip", on: boolean) => {
    setProblem(null);
    setState(
      (current) =>
        current && {
          vpn: { ...current.vpn, ...(key === "build_proxy_enabled" ? { enabled: on } : key === "vpn_change_ip" ? { change_ip: on } : {}) },
          solver: { ...current.solver, ...(key === "captcha_solver_enabled" ? { enabled: on } : {}) },
        },
    );
    return api("/api/settings", { method: "PUT", body: JSON.stringify({ [key]: on }) })
      .then(() => load(true))
      .catch((reason) => {
        setProblem(String(reason.message));
        load(true);
      });
  };
  const changeIp = () => {
    setBusy(true);
    setProblem(null);
    return post<NetworkState & { changed: boolean }>("/api/network/change-ip")
      .then((result) => {
        setState({ vpn: result.vpn, solver: result.solver });
        if (!result.changed) setProblem(result.vpn.last_change_error || "The VPN reconnected with the same address as before.");
      })
      .catch((reason) => {
        setProblem(String(reason.message));
        load(true);
      })
      .finally(() => setBusy(false));
  };
  return { state, problem, busy, load, turn, changeIp };
}

const REFRESH = "M15.5 8A6 6 0 0 0 4.6 7M4.5 12a6 6 0 0 0 10.9 1M15.5 4v4h-4M4.5 16v-4h4";
const GLOW: Record<Tone, string> = {
  green: "bg-emerald-500 shadow-[0_0_6px_1px] shadow-emerald-500/60",
  amber: "bg-amber-500 shadow-[0_0_6px_1px] shadow-amber-500/60",
  red: "bg-red-500 shadow-[0_0_6px_1px] shadow-red-500/60",
  grey: "bg-zinc-300 dark:bg-zinc-600",
};

/** A status light: lit in the colour of the state, dark when the thing is off. */
function Led({ tone, pulse = false }: { tone: Tone; pulse?: boolean }) {
  return <span className={`h-2 w-2 shrink-0 rounded-full ${GLOW[tone]} ${pulse ? "animate-pulse" : ""}`} aria-hidden />;
}

function Fact({ label, children, mono = false }: { label: string; children: ReactNode; mono?: boolean }) {
  if (!children) return null;
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="muted shrink-0">{label}</dt>
      <dd className={`min-w-0 truncate text-right ${mono ? "font-mono" : ""}`} title={typeof children === "string" ? children : undefined}>
        {children}
      </dd>
    </div>
  );
}

/** One line of the menu's foot: a light, a name, the "i" that opens the details, a switch. */
function Row({
  name,
  about,
  large,
  open,
  onOpen,
  toggle,
  children,
}: {
  name: string;
  about: { tone: Tone; title: string; line: string };
  large: boolean;
  open: boolean;
  onOpen: (open: boolean) => void;
  toggle: ReactNode;
  children: ReactNode;
}) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (event: PointerEvent) => !box.current?.contains(event.target as Node) && onOpen(false);
    const key = (event: KeyboardEvent) => event.key === "Escape" && onOpen(false);
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", key);
    };
  }, [open, onOpen]);
  return (
    <div ref={box} className={`relative flex items-center gap-2 ${large ? "px-3.5 py-2.5" : "px-2.5 py-1.5"}`}>
      <Led tone={about.tone} pulse={about.tone === "amber"} />
      <span className="min-w-0 flex-1 truncate font-medium" title={`${name}: ${about.title}`}>
        {name}
        <span className="sr-only">: {about.title}</span>
      </span>
      <button
        type="button"
        className={`-m-1 shrink-0 rounded-full p-1 transition ${open ? "text-brand-700 dark:text-brand-400" : "text-zinc-400 hover:text-zinc-700 dark:text-zinc-500 dark:hover:text-zinc-200"}`}
        onClick={() => onOpen(!open)}
        aria-expanded={open}
        aria-label={`${name} details`}
        title={`${about.title}. Show the details`}
      >
        <svg viewBox="0 0 20 20" className={large ? "h-[18px] w-[18px]" : "h-4 w-4"} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" aria-hidden>
          <circle cx="10" cy="10" r="7.25" />
          <path d="M10 9.25v4.25M10 6.5h.01" />
        </svg>
      </button>
      {toggle}
      {open && (
        <div
          role="dialog"
          aria-label={`${name} details`}
          className={`rise absolute z-50 rounded-xl border border-zinc-200 bg-white p-3 text-xs shadow-xl dark:border-zinc-700 dark:bg-zinc-900 ${
            large ? "inset-x-0 bottom-full mb-2 text-sm" : "bottom-0 left-full ml-4 w-64"
          }`}
        >
          <div className="flex items-center gap-2">
            <Led tone={about.tone} pulse={about.tone === "amber"} />
            <span className="min-w-0 flex-1 truncate text-sm font-semibold">
              {name} <span className="muted font-normal">· {about.title}</span>
            </span>
          </div>
          {children}
          <Link to="/settings#network" className="text-brand-700 dark:text-brand-400 mt-2.5 inline-block font-medium hover:underline" onClick={() => onOpen(false)}>
            Open settings
          </Link>
        </div>
      )}
    </div>
  );
}

/**
 * The foot of the menu: a light and a switch each for the VPN and the solver, with the
 * details behind the "i". Shown only for what has been set up in Settings.
 */
export function NetworkPanel({ large = false }: { large?: boolean }) {
  const { state, problem, busy, turn, changeIp } = useNetwork();
  const [open, setOpen] = useState<"vpn" | "solver" | null>(null);
  const openVpn = useCallback((on: boolean) => setOpen(on ? "vpn" : null), []);
  const openSolver = useCallback((on: boolean) => setOpen(on ? "solver" : null), []);
  if (!state) return null;
  const { vpn, solver } = state;
  const showVpn = vpn.proxy_set || vpn.control;
  if (!showVpn && !solver.set) return null;
  const about = vpnSummary(vpn);
  const check = solverSummary(solver);
  const changing = busy || vpn.changing;
  const connected = vpn.proxy_set && vpn.enabled && vpn.control && !vpn.error && !!vpn.public_ip;
  const note = (tone: Tone) => `mt-2 ${tone === "red" ? "text-red-600 dark:text-red-400" : "muted"}`;
  return (
    <section
      className={`divide-y divide-zinc-200 rounded-xl border border-zinc-200 bg-zinc-50 dark:divide-zinc-800 dark:border-zinc-800 dark:bg-zinc-950/40 ${large ? "text-sm" : "text-xs"}`}
      aria-label="Network"
    >
      {showVpn && (
        <Row
          name="VPN"
          about={about}
          large={large}
          open={open === "vpn"}
          onOpen={openVpn}
          toggle={<Switch small={!large} checked={vpn.enabled} onChange={(on) => turn("build_proxy_enabled", on)} label="Send requests through the VPN" disabled={!vpn.proxy_set} />}
        >
          {connected ? (
            <dl className="mt-2.5 space-y-1">
              <Fact label="IP address" mono>
                {vpn.public_ip}
              </Fact>
              <Fact label="Country">{vpn.country}</Fact>
              <Fact label="Region">{[vpn.city, vpn.region].filter(Boolean).join(", ")}</Fact>
              <Fact label="Network">{vpn.organization}</Fact>
              <Fact label="Changes">{vpn.changes ? `${vpn.changes}, last ${ago(vpn.last_change)}` : ""}</Fact>
            </dl>
          ) : (
            about.line && <p className={note(about.tone)}>{about.line}</p>
          )}
          {problem && <p className={note("red")}>{problem}</p>}
          {connected && (
            <button type="button" className="btn mt-2.5 w-full justify-center" onClick={changeIp} disabled={changing} title="Reconnect the VPN for another address">
              <svg viewBox="0 0 20 20" className={`h-3.5 w-3.5 ${changing ? "animate-spin" : ""}`} fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                <path d={REFRESH} />
              </svg>
              {changing ? "Changing IP…" : "Change IP"}
            </button>
          )}
        </Row>
      )}
      {solver.set && (
        <Row
          name={large ? "Captcha solver" : "Captcha"}
          about={check}
          large={large}
          open={open === "solver"}
          onOpen={openSolver}
          toggle={<Switch small={!large} checked={solver.enabled} onChange={(on) => turn("captcha_solver_enabled", on)} label="Use the captcha solver" />}
        >
          {check.line && <p className={note(check.tone)}>{check.line}</p>}
          {!showVpn && problem && <p className={note("red")}>{problem}</p>}
        </Row>
      )}
    </section>
  );
}

function Detail({ label, children, mono = false }: { label: string; children: ReactNode; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="muted text-xs">{label}</dt>
      <dd className={`truncate text-sm ${mono ? "font-mono" : ""}`} title={typeof children === "string" ? children : undefined}>
        {children || "—"}
      </dd>
    </div>
  );
}

/** For Settings: what the saved VPN settings give right now, in full. */
export function VpnStatus() {
  const { state, problem, busy, changeIp } = useNetwork();
  if (!state) return null;
  const { vpn } = state;
  if (!vpn.proxy_set && !vpn.control) return null;
  const about = vpnSummary(vpn);
  const changing = busy || vpn.changing;
  return (
    <div className="my-3 rounded-xl border border-zinc-200 bg-zinc-50 p-3.5 dark:border-zinc-800 dark:bg-zinc-950/40">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <Dot tone={about.tone} pulse={about.tone === "amber"} />
        <div className="min-w-0 flex-1 basis-48">
          <div className="text-sm font-medium">{about.title}</div>
          {about.line && <div className={`text-xs ${about.tone === "red" ? "text-red-600 dark:text-red-400" : "muted"}`}>{about.line}</div>}
        </div>
        <div className="flex flex-wrap gap-2">
          {vpn.control && (
            <button type="button" className="btn" onClick={changeIp} disabled={changing || !vpn.enabled || !vpn.proxy_set} title={vpn.enabled && vpn.proxy_set ? "Reconnect the VPN for another address" : "Switch the VPN on first"}>
              {changing ? "Changing…" : "Change IP now"}
            </button>
          )}
        </div>
      </div>
      {vpn.control && vpn.public_ip && (
        <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2.5 border-t border-zinc-200 pt-3 sm:grid-cols-3 dark:border-zinc-800">
          <Detail label="IP address" mono>
            {vpn.public_ip}
          </Detail>
          <Detail label="Country">{vpn.country}</Detail>
          <Detail label="Region">{[vpn.city, vpn.region].filter(Boolean).join(", ")}</Detail>
          <Detail label="Network">{vpn.organization}</Detail>
          <Detail label="Time zone">{vpn.timezone}</Detail>
          <Detail label="Address changes">{vpn.changes ? `${vpn.changes}, last ${ago(vpn.last_change)}` : "None since the server started"}</Detail>
        </dl>
      )}
      {problem && <p className="mt-2 text-xs text-red-600 dark:text-red-400">{problem}</p>}
      <p className="muted mt-2 text-xs">What the saved settings give right now.</p>
    </div>
  );
}

export function SolverStatus() {
  const { state } = useNetwork();
  if (!state?.solver.set) return null;
  const check = solverSummary(state.solver);
  return (
    <div className="my-3 flex items-center gap-3 rounded-xl border border-zinc-200 bg-zinc-50 p-3.5 dark:border-zinc-800 dark:bg-zinc-950/40">
      <Dot tone={check.tone} pulse={check.tone === "amber"} />
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium">{check.title}</div>
        {check.line && <div className={`text-xs ${check.tone === "red" ? "text-red-600 dark:text-red-400" : "muted"}`}>{check.line}</div>}
      </div>
    </div>
  );
}

interface VpnTry {
  control: (Partial<Vpn> & { ok: boolean; error: string; ms: number }) | null;
  proxy: { ok: boolean; ip: string; country: string; error: string; ms: number } | null;
  same_address: boolean | null;
}
type Typed = { build_proxy: string; vpn_control_url: string; vpn_control_key: string; vpn_control_user: string; vpn_control_password: string; captcha_solver_url: string };

function Outcome({ ok, title, children }: { ok: boolean | null; title: string; children: ReactNode }) {
  const tone = ok === null ? "text-amber-600 dark:text-amber-400" : ok ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400";
  return (
    <li className="flex gap-2.5 py-1.5">
      <svg viewBox="0 0 20 20" className={`mt-0.5 h-4 w-4 shrink-0 ${tone}`} fill="none" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        <path d={ok === null ? "M10 6v5M10 14h.01" : ok ? "M4.5 10.5l3.5 3.500 7.500-8" : "M5.500 5.500l9 9M14.500 5.500l-9 9"} />
      </svg>
      <div className="min-w-0 text-sm">
        <span className="font-medium">{title}</span> <span className={ok === false ? "text-red-600 dark:text-red-400" : "muted"}>{children}</span>
      </div>
    </li>
  );
}

function TestButton({ testing, onClick, disabled }: { testing: boolean; onClick: () => void; disabled: boolean }) {
  return (
    <button type="button" className="btn" onClick={onClick} disabled={testing || disabled}>
      {testing ? "Testing…" : "Test connection"}
    </button>
  );
}

/** Try the VPN's addresses as they are in the form, saved or not. */
export function VpnTest({ typed }: { typed: Typed }) {
  const [result, setResult] = useState<VpnTry | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const run = () => {
    setTesting(true);
    setProblem(null);
    setResult(null);
    post<VpnTry>("/api/network/test", { kind: "vpn", ...typed })
      .then(setResult)
      .catch((reason) => setProblem(String(reason.message)))
      .finally(() => setTesting(false));
  };
  const { control, proxy } = result ?? { control: null, proxy: null };
  return (
    <div className="py-3">
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-1.5">
        <span className="min-w-0 flex-1 basis-64">
          <span className="block text-sm font-medium">Test the connection</span>
          <span className="muted block text-xs">Tries the addresses above as typed, saved or not: asks the control server for its state, and fetches a page through the proxy (from cloudflare.com, which answers with the address it was asked from).</span>
        </span>
        <TestButton testing={testing} onClick={run} disabled={!typed.build_proxy.trim() && !typed.vpn_control_url.trim()} />
      </div>
      {problem && <p className="mt-2 text-sm text-red-600 dark:text-red-400">{problem}</p>}
      {result && (
        <ul className="mt-2 rounded-xl border border-zinc-200 bg-zinc-50 px-3.5 py-2 dark:border-zinc-800 dark:bg-zinc-950/40" aria-label="Test results">
          {control && (
            <Outcome ok={control.ok && control.status === "running"} title="Control server:">
              {control.ok
                ? control.status === "running"
                  ? `answers in ${control.ms} ms. VPN connected${control.public_ip ? ` as ${control.public_ip}${control.country ? `, ${[control.city, control.country].filter(Boolean).join(", ")}` : ""}` : ", address not known yet"}.`
                  : `answers, but the VPN is ${control.status || "not running"}.`
                : control.error}
            </Outcome>
          )}
          {!control && <Outcome ok={null} title="Control server:">no address typed, so the connection cannot be shown or changed.</Outcome>}
          {proxy && (
            <Outcome ok={proxy.ok} title="Proxy:">
              {proxy.ok ? `works, in ${proxy.ms} ms. Sites see ${proxy.ip}${proxy.country ? ` (${proxy.country})` : ""}.` : proxy.error}
            </Outcome>
          )}
          {!proxy && <Outcome ok={null} title="Proxy:">no address typed, so nothing is sent through the VPN.</Outcome>}
          {result.same_address !== null && (
            <Outcome ok={result.same_address} title="Same address:">
              {result.same_address ? "the proxy leaves from the VPN's address." : "the proxy does not leave from the address the control server reports. They may belong to different containers."}
            </Outcome>
          )}
        </ul>
      )}
    </div>
  );
}

/** Try the solver's address as it is in the form, saved or not. */
export function SolverTest({ address }: { address: string }) {
  const [result, setResult] = useState<Solver | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const run = () => {
    setTesting(true);
    setProblem(null);
    setResult(null);
    post<{ solver: Solver }>("/api/network/test", { kind: "solver", captcha_solver_url: address })
      .then((reply) => setResult(reply.solver))
      .catch((reason) => setProblem(String(reason.message)))
      .finally(() => setTesting(false));
  };
  return (
    <div className="py-3">
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-1.5">
        <span className="min-w-0 flex-1 basis-64">
          <span className="block text-sm font-medium">Test the connection</span>
          <span className="muted block text-xs">Asks the address above, saved or not, whether FlareSolverr is there and ready.</span>
        </span>
        <TestButton testing={testing} onClick={run} disabled={!address.trim()} />
      </div>
      {problem && <p className="mt-2 text-sm text-red-600 dark:text-red-400">{problem}</p>}
      {result && (
        <ul className="mt-2 rounded-xl border border-zinc-200 bg-zinc-50 px-3.5 py-2 dark:border-zinc-800 dark:bg-zinc-950/40" aria-label="Test results">
          <Outcome ok={result.ready} title="Captcha solver:">
            {result.ready ? `ready${result.version ? `, FlareSolverr ${result.version.replace(/^v/, "")}` : ""}.` : result.error}
          </Outcome>
        </ul>
      )}
    </div>
  );
}
