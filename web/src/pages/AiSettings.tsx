import { useMemo, useState, type ReactNode } from "react";
import { Combobox } from "../Combobox";
import { Dialog } from "../Dialog";
import { KIND_NAMES, modelKey, modelOptions, PRESETS, splitModelKey, type ModelGroup, type ProviderInfo } from "../lib/ai";
import { api, authHeaders, post } from "../lib/api";
import { useTouch } from "../lib/viewport";

export const PICKER = "input flex w-full items-center gap-2.5 text-left";
const MASK = "••••••••";

export function Field({
  label,
  hint,
  group = false,
  children,
}: {
  label: string;
  hint?: ReactNode;
  /** For a custom control: a plain row, since a <label> would forward clicks on its text to it. */
  group?: boolean;
  children: ReactNode;
}) {
  const Row = group ? "div" : "label";
  return (
    <Row className="flex flex-wrap items-center justify-between gap-x-6 gap-y-1.5 py-3">
      <span className="min-w-0 flex-1 basis-64">
        <span className="block text-sm font-medium">{label}</span>
        {hint && <span className="muted block text-xs">{hint}</span>}
      </span>
      {children}
    </Row>
  );
}

export function Segmented<T extends string>({
  value,
  onChange,
  options,
  label,
}: {
  value: T;
  onChange: (value: T) => void;
  options: { value: T; label: string }[];
  label: string;
}) {
  return (
    <div className="inline-flex rounded-lg bg-zinc-100 p-0.5 dark:bg-zinc-800" role="radiogroup" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={value === option.value}
          onClick={() => onChange(option.value)}
          className={`rounded-md px-3 py-1 text-sm font-medium whitespace-nowrap transition ${
            value === option.value
              ? "bg-white text-zinc-900 shadow-sm dark:bg-zinc-600 dark:text-white"
              : "text-zinc-600 hover:text-zinc-900 dark:text-zinc-300 dark:hover:text-white"
          }`}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

interface Draft {
  id: string;
  name: string;
  kind: string;
  base_url: string;
  api_key: string;
}
type Probe = { state: "busy" | "ok" | "failed"; text: string } | null;
const host = (url: string) => url.replace(/^https?:\/\//, "").replace(/\/.*$/, "");
const LABEL = "mb-1.5 block text-sm font-medium";
const HINT = "muted mt-1.5 block text-xs leading-relaxed";

function Tile({ badge, name, about, onClick }: { badge: string; name: string; about: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="hover:border-brand-500 flex items-center gap-3 rounded-2xl border border-zinc-200 p-3 text-left transition hover:shadow-sm active:scale-[0.98] dark:border-zinc-800"
    >
      <span className="bg-brand-100 text-brand-700 dark:bg-brand-900 dark:text-brand-100 flex h-10 w-10 shrink-0 items-center justify-center rounded-xl text-base font-semibold" aria-hidden>
        {badge}
      </span>
      <span className="min-w-0">
        <span className="block truncate text-sm font-medium">{name}</span>
        <span className="muted block truncate text-xs">{about}</span>
      </span>
    </button>
  );
}

/** Add or edit one provider: pick the service, paste the key, done. */
function ProviderDialog({ initial, onClose, onSaved }: { initial: Draft | null; onClose: () => void; onSaved: () => void }) {
  const touch = useTouch();
  const [draft, setDraft] = useState<Draft | null>(initial);
  const [probe, setProbe] = useState<Probe>(null);
  const [advanced, setAdvanced] = useState(false);
  const editing = !!initial?.id;
  const preset = PRESETS.find((p) => p.kind === draft?.kind);
  const patch = (change: Partial<Draft>) => (setDraft((d) => d && { ...d, ...change }), setProbe(null));

  const pick = (kind: string) => {
    const chosen = PRESETS.find((p) => p.kind === kind);
    setProbe(null);
    setAdvanced(!chosen);
    setDraft({ id: "", kind, name: chosen?.name ?? "", base_url: chosen?.url ?? "", api_key: "" });
  };
  const test = async (): Promise<boolean> => {
    if (!draft) return false;
    setProbe({ state: "busy", text: "Connecting…" });
    try {
      const result = await post<{ models: string[] }>("/api/ai/providers/test", draft);
      const count = result.models.length;
      setProbe({ state: "ok", text: `Connected. ${count} model${count === 1 ? "" : "s"} available.` });
      return true;
    } catch (reason) {
      setProbe({ state: "failed", text: String((reason as Error).message) });
      return false;
    }
  };
  const save = async (force = false) => {
    if (!draft) return;
    // Check the details work before saving, so a typo in the key is caught here and not
    // later in the middle of a chat. "Save anyway" is there for a server that is just off.
    if (!force && !(await test())) return;
    try {
      const body = JSON.stringify(draft);
      await api(draft.id ? `/api/ai/providers/${draft.id}` : "/api/ai/providers", { method: draft.id ? "PUT" : "POST", body });
      onSaved();
    } catch (reason) {
      setProbe({ state: "failed", text: String((reason as Error).message) });
    }
  };
  const remove = async () => {
    if (!draft || !window.confirm(`Remove ${draft.name}? Its key is deleted. Chats keep their text.`)) return;
    await api(`/api/ai/providers/${draft.id}`, { method: "DELETE" });
    onSaved();
  };

  if (!draft) {
    return (
      <Dialog title="Add a provider" onClose={onClose}>
        {(
          [
            ["Free, no key needed", PRESETS.filter((p) => p.free)],
            ["Hosted services", PRESETS.filter((p) => !p.local && !p.free)],
            ["On this machine", PRESETS.filter((p) => p.local)],
          ] as const
        ).map(([heading, items]) => (
          <div key={heading} className="mb-5">
            <h3 className="muted mb-2 text-xs font-semibold tracking-wider uppercase">{heading}</h3>
            <div className="grid gap-2 sm:grid-cols-2">
              {items.map((p) => (
                <Tile key={p.kind} badge={p.name.charAt(0)} name={p.name} about={p.about} onClick={() => pick(p.kind)} />
              ))}
            </div>
          </div>
        ))}
        <h3 className="muted mb-2 text-xs font-semibold tracking-wider uppercase">Something else</h3>
        <Tile badge="…" name="Custom server" about="Any OpenAI-compatible address" onClick={() => pick("custom")} />
      </Dialog>
    );
  }

  const ready = !!draft.name.trim() && !!draft.base_url.trim();
  const busy = probe?.state === "busy";
  const showAddress = advanced || !preset;
  const keyless = !!preset && (preset.local || preset.free);
  return (
    <Dialog
      title={editing ? `Edit ${initial!.name}` : (preset?.name ?? "Custom server")}
      onClose={onClose}
      onBack={editing ? undefined : () => (setDraft(null), setProbe(null))}
      footer={
        <div className="space-y-2.5">
          {probe && (
            <p
              role="status"
              className={`rounded-xl px-3 py-2 text-sm ${
                probe.state === "failed"
                  ? "bg-red-50 text-red-800 dark:bg-red-950 dark:text-red-300"
                  : probe.state === "ok"
                    ? "bg-brand-50 text-brand-900 dark:bg-brand-900/40 dark:text-brand-100"
                    : "muted bg-zinc-100 dark:bg-zinc-800"
              }`}
            >
              {probe.text}
            </p>
          )}
          <div className="flex gap-2">
            <button type="button" className="btn flex-1 sm:flex-none" disabled={!draft.base_url.trim() || busy} onClick={() => void test()}>
              Test
            </button>
            <span className="hidden flex-1 sm:block" />
            {probe?.state === "failed" && (
              <button type="button" className="btn flex-1 sm:flex-none" disabled={!ready} onClick={() => void save(true)}>
                Save anyway
              </button>
            )}
            <button type="button" className="btn btn-primary flex-[2] sm:flex-none" disabled={!ready || busy} onClick={() => void save()}>
              {editing ? "Save" : "Add provider"}
            </button>
          </div>
        </div>
      }
    >
      <div className="space-y-5">
        {!keyless && (
          <label className="block">
            <span className={LABEL}>API key</span>
            <input
              type="password"
              autoFocus={!touch}
              autoComplete="off"
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
              enterKeyHint="done"
              className="input"
              placeholder={preset ? "Paste your key" : "Leave empty if the server needs none"}
              value={draft.api_key}
              onFocus={(e) => e.target.select()}
              onChange={(e) => patch({ api_key: e.target.value })}
              aria-label="API key"
            />
            <span className={HINT}>
              {preset?.keys && (
                <>
                  Get one at{" "}
                  <a className="text-brand-700 dark:text-brand-400 underline" href={preset.keys} target="_blank" rel="noreferrer">
                    {host(preset.keys)}
                  </a>
                  .{" "}
                </>
              )}
              It is stored on this server and never shown again.
            </span>
          </label>
        )}
        {keyless && (
          <p className="rounded-xl bg-zinc-100 px-3 py-2.5 text-sm leading-relaxed text-zinc-700 dark:bg-zinc-800 dark:text-zinc-200">
            {preset?.note ?? `${preset?.name} needs no key. It must be running on this machine and listening on all interfaces, not only on localhost.`}
          </p>
        )}
        <label className="block">
          <span className={LABEL}>Name</span>
          <input className="input" placeholder="My server" value={draft.name} onChange={(e) => patch({ name: e.target.value })} aria-label="Provider name" enterKeyHint="done" />
          <span className={HINT}>How it appears in the model picker.</span>
        </label>
        {showAddress ? (
          <label className="block">
            <span className={LABEL}>Server address</span>
            <input
              type="url"
              inputMode="url"
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
              enterKeyHint="done"
              className="input font-mono !text-sm"
              placeholder="https://example.com/v1"
              value={draft.base_url}
              onChange={(e) => patch({ base_url: e.target.value })}
              aria-label="Server address"
            />
            <span className={HINT}>
              The API base URL, usually ending in /v1. From Docker, a server on this machine is <code>host.docker.internal</code>, not localhost.
            </span>
          </label>
        ) : (
          <button type="button" className="text-brand-700 dark:text-brand-400 text-sm hover:underline" onClick={() => setAdvanced(true)}>
            Change the server address ({host(draft.base_url)})
          </button>
        )}
        {keyless && advanced && (
          <label className="block">
            <span className={LABEL}>API key (optional)</span>
            <input type="password" autoComplete="off" className="input" placeholder="Not needed" value={draft.api_key} onChange={(e) => patch({ api_key: e.target.value })} aria-label="API key" />
          </label>
        )}
        {editing && (
          <button type="button" className="text-sm text-red-600 hover:underline dark:text-red-400" onClick={remove}>
            Remove this provider
          </button>
        )}
      </div>
    </Dialog>
  );
}

/** The list of connected providers. Each change is saved on its own. */
export function Providers({ groups, onChanged }: { groups: ModelGroup[] | null; onChanged: () => void }) {
  // undefined: closed. null: adding, at the "pick a service" step. Otherwise: editing.
  const [dialog, setDialog] = useState<Draft | null | undefined>(undefined);
  const [switching, setSwitching] = useState("");
  const toggle = (provider: ProviderInfo) => {
    setSwitching(provider.id);
    api(`/api/ai/providers/${provider.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !provider.enabled }) })
      .then(onChanged)
      .finally(() => setSwitching(""));
  };
  const edit = (provider: ProviderInfo) =>
    setDialog({ id: provider.id, name: provider.name, kind: provider.kind, base_url: provider.base_url, api_key: provider.has_key ? MASK : "" });

  return (
    <section id="ask-ai" className="card scroll-mt-16 px-4">
      <div className="flex flex-wrap items-center justify-between gap-3 py-3">
        <div className="min-w-0 flex-1 basis-64">
          <h2 className="font-medium">AI providers</h2>
          <p className="muted text-xs">
            Connect as many as you like, hosted or local, and switch between their models in the chat box. Questions and the
            passages used to answer them are sent to the provider you pick.
          </p>
        </div>
        {!!groups?.length && (
          <button className="btn btn-primary w-full sm:w-auto" onClick={() => setDialog(null)}>
            Add provider
          </button>
        )}
      </div>

      {groups && groups.length === 0 && (
        <div className="border-t border-zinc-100 py-8 text-center dark:border-zinc-800">
          <p className="font-medium">No provider yet</p>
          <p className="muted mx-auto mt-1 max-w-sm text-sm">Add OpenAI, Google Gemini, a local Ollama or any OpenAI-compatible server to start using Ask AI.</p>
          <button className="btn btn-primary mt-4" onClick={() => setDialog(null)}>
            Add your first provider
          </button>
        </div>
      )}
      <ul className="divide-y divide-zinc-100 border-t border-zinc-100 empty:hidden dark:divide-zinc-800 dark:border-zinc-800">
        {groups?.map(({ provider, models, error }) => (
          <li key={provider.id} className="flex items-center gap-1">
            <button className={`-ml-2 flex min-w-0 flex-1 items-center gap-3 rounded-xl px-2 py-3 text-left transition hover:bg-zinc-50 dark:hover:bg-zinc-800/50 ${provider.enabled ? "" : "opacity-55"}`} onClick={() => edit(provider)} aria-label={`Edit ${provider.name}`}>
              <span className="bg-brand-100 text-brand-700 dark:bg-brand-900 dark:text-brand-100 flex h-10 w-10 shrink-0 items-center justify-center rounded-xl font-semibold" aria-hidden>
                {provider.name.charAt(0).toUpperCase()}
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="truncate text-sm font-medium">{provider.name}</span>
                  {provider.free ? (
                    <span className="badge bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300">Free</span>
                  ) : (
                    KIND_NAMES[provider.kind] !== provider.name && <span className="badge">{KIND_NAMES[provider.kind] ?? provider.kind}</span>
                  )}
                </span>
                <span className={`block truncate text-xs ${error && provider.enabled ? "text-red-600 dark:text-red-400" : "muted"}`}>
                  {!provider.enabled
                    ? "Turned off · kept, but not used"
                    : (error ??
                    `${models.length} model${models.length === 1 ? "" : "s"} · ${
                      provider.free ? "free, no key, rate-limited" : provider.has_key ? "key saved" : "no key"
                    }${provider.web ? " · can search the web" : ""}`)}
                </span>
              </span>
              <svg viewBox="0 0 20 20" className="muted h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                <path d="m8 4.5 5.5 5.5L8 15.5" />
              </svg>
            </button>
            <button
              type="button"
              role="switch"
              aria-checked={provider.enabled}
              aria-label={`Use ${provider.name}`}
              title={provider.enabled ? "On. Turn off to stop using it without removing it." : "Off. Turn on to use it again."}
              disabled={switching === provider.id}
              onClick={() => toggle(provider)}
              className={`relative h-6 w-11 shrink-0 rounded-full transition disabled:opacity-60 ${provider.enabled ? "bg-brand-600" : "bg-zinc-300 dark:bg-zinc-700"}`}
            >
              <span className={`absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${provider.enabled ? "translate-x-5" : ""}`} />
            </button>
          </li>
        ))}
      </ul>

      {dialog !== undefined && <ProviderDialog initial={dialog} onClose={() => setDialog(undefined)} onSaved={() => (setDialog(undefined), onChanged())} />}
    </section>
  );
}

/** A provider-and-model picker bound to two settings keys. */
export function ModelPicker({
  groups,
  provider,
  model,
  onChange,
  only,
  placeholder = "Choose a model",
}: {
  groups: ModelGroup[] | null;
  provider: string;
  model: string;
  onChange: (provider: string, model: string) => void;
  only?: (provider: ProviderInfo) => boolean;
  placeholder?: string;
}) {
  const options = useMemo(() => {
    const listed = modelOptions(groups ?? [], only);
    const current = modelKey(provider, model);
    // Keep showing a saved choice even if its provider is unreachable right now.
    if (current && !listed.some((o) => o.value === current)) listed.unshift({ value: current, label: model.replace(/^models\//, "") });
    return listed;
  }, [groups, provider, model, only]);
  return (
    <div className="w-full sm:w-96">
      <Combobox
        wide
        value={modelKey(provider, model)}
        options={options}
        placeholder={placeholder}
        searchPlaceholder={`Search ${options.length} models…`}
        className={PICKER}
        onChange={(key) => onChange(...splitModelKey(key))}
      />
    </div>
  );
}

/** Speak a sample with the given voice settings, to check they work before saving. */
export async function playSample(provider: string, model: string, voice: string): Promise<void> {
  const response = await fetch("/api/ai/speech", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ text: "This is how answers will sound when read aloud.", provider, model, voice }),
  });
  if (!response.ok) throw new Error((await response.json().catch(() => null))?.detail ?? `HTTP ${response.status}`);
  const url = URL.createObjectURL(await response.blob());
  const audio = new Audio(url);
  audio.onended = () => URL.revokeObjectURL(url);
  await audio.play();
}
