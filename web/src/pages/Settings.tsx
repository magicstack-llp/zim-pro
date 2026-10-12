import { useEffect, useMemo, useRef, useState } from "react";
import { Combobox, type Option } from "../Combobox";
import { api, post } from "../lib/api";
import { bytes } from "../lib/format";
import { useData, useLive } from "../lib/live";
import { ErrorNote, PageHeader } from "../ui";

interface SettingsData {
  max_concurrent_downloads: number;
  connections_per_file: number;
  bandwidth_limit_kbps: number;
  catalog_refresh_hours: number;
  default_policy: string;
  keep_old_versions: boolean;
  mcp_allow_writes: boolean;
  ai_base_url: string;
  ai_api_key: string;
  ai_model: string;
  ai_max_sources: number;
}

interface Provider {
  id: string;
  name: string;
  url: string;
  about: string;
  local?: boolean;
}

const PROVIDERS: Provider[] = [
  { id: "openai", name: "OpenAI", url: "https://api.openai.com/v1", about: "Hosted · needs an API key" },
  { id: "openrouter", name: "OpenRouter", url: "https://openrouter.ai/api/v1", about: "Hosted · many models behind one key" },
  { id: "groq", name: "Groq", url: "https://api.groq.com/openai/v1", about: "Hosted · needs an API key" },
  { id: "ollama", name: "Ollama", url: "http://host.docker.internal:11434/v1", about: "On this machine · no key", local: true },
  { id: "lmstudio", name: "LM Studio", url: "http://host.docker.internal:1234/v1", about: "On this machine · no key", local: true },
  { id: "llamacpp", name: "llama.cpp server", url: "http://host.docker.internal:8080/v1", about: "On this machine · no key", local: true },
];
const CUSTOM = "custom";

const PROVIDER_OPTIONS: Option[] = [
  ...PROVIDERS.map((p) => ({
    value: p.id,
    label: p.name,
    description: p.about,
    badge: p.name.charAt(0),
    group: p.local ? "Local" : "Hosted",
  })),
  { value: CUSTOM, label: "Custom server", description: "Any other OpenAI-compatible address", badge: "…", group: "Other" },
];

const sameUrl = (a: string, b: string) => a.trim().replace(/\/+$/, "") === b.replace(/\/+$/, "");
const PICKER = "input flex w-full items-center gap-2.5 text-left";

interface Leftover {
  path: string;
  name: string;
  size: number;
  superseded_by?: string;
}

function Field({
  label,
  hint,
  group = false,
  children,
}: {
  label: string;
  hint?: string;
  /** For a custom picker: a plain row, since a <label> would forward clicks on its text to it. */
  group?: boolean;
  children: React.ReactNode;
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

export default function Settings() {
  const { refresh } = useLive();
  const loaded = useData<SettingsData>("/api/settings");
  const leftovers = useData<Leftover[]>("/api/library/leftovers");
  const [form, setForm] = useState<SettingsData | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [probe, setProbe] = useState<{ ok: boolean; text: string } | null>(null);
  const [saved, setSaved] = useState("");
  const [customProvider, setCustomProvider] = useState(false);
  const urlInput = useRef<HTMLInputElement>(null);
  const testAi = async (quiet = false) => {
    if (!quiet) setProbe({ ok: true, text: "Connecting…" });
    try {
      const result = await post<{ models: string[] }>("/api/ai/models", {
        base_url: form?.ai_base_url ?? "",
        api_key: form?.ai_api_key ?? "",
      });
      setModels(result.models);
      if (quiet) return;
      const count = result.models.length;
      setProbe({ ok: true, text: `Connected · ${count} model${count === 1 ? "" : "s"} available` });
    } catch (reason) {
      setModels([]);
      if (!quiet) setProbe({ ok: false, text: String((reason as Error).message) });
    }
  };
  const provider = PROVIDERS.find((p) => sameUrl(form?.ai_base_url ?? "", p.url));
  const providerId = !customProvider && provider ? provider.id : form?.ai_base_url || customProvider ? CUSTOM : "";
  const pickProvider = (id: string) => {
    const preset = PROVIDERS.find((p) => p.id === id);
    setCustomProvider(!preset);
    setProbe(null);
    setModels([]);
    if (preset) set("ai_base_url", preset.url);
    else setTimeout(() => urlInput.current?.focus(), 0);
  };
  const modelOptions = useMemo<Option[]>(() => models.map((model) => ({ value: model, label: model })), [models]);
  // Fill the model list for a server that is already set up, without reporting failures.
  useEffect(() => {
    if (loaded.data?.ai_base_url) void testAi(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded.data?.ai_base_url]);
  useEffect(() => {
    if (window.location.hash) document.querySelector(window.location.hash)?.scrollIntoView();
  }, [form === null]);
  useEffect(() => {
    if (loaded.data && !form) {
      setForm(loaded.data);
      setSaved(JSON.stringify(loaded.data));
    }
  }, [loaded.data, form]);

  const set = <K extends keyof SettingsData>(key: K, value: SettingsData[K]) => {
    setForm((current) => current && { ...current, [key]: value });
    setNote(null);
  };
  const number = (key: keyof SettingsData, min: number, max?: number) => (
    <input
      type="number"
      className="input w-28"
      min={min}
      max={max}
      value={form ? Number(form[key]) : 0}
      onChange={(e) => set(key, Number(e.target.value) as never)}
    />
  );
  const save = async () => {
    setProblem(null);
    try {
      const stored = await api<SettingsData>("/api/settings", { method: "PUT", body: JSON.stringify(form) });
      setForm(stored);
      setSaved(JSON.stringify(stored));
      setNote("Saved");
      refresh();
    } catch (reason) {
      setProblem(String((reason as Error).message));
    }
  };
  const rescan = async () => {
    setProblem(null);
    try {
      const result = await post<{ books: number; read: number; removed: number }>("/api/library/rescan");
      setNote(`Rescanned: ${result.books} books, ${result.read} new or changed, ${result.removed} removed`);
      refresh();
    } catch (reason) {
      setProblem(String((reason as Error).message));
    }
  };
  const mcpUrl = `${window.location.origin}/mcp`;
  const dirty = !!form && JSON.stringify(form) !== saved;

  return (
    <>
      <PageHeader title="Settings" />
      <ErrorNote message={loaded.error ?? problem} />
      {form && (
        <div className="space-y-5">
          <section className="card divide-y divide-zinc-100 px-4 dark:divide-zinc-800">
            <h2 className="py-3 font-medium">Downloads</h2>
            <Field label="Files at once" hint="How many books download in parallel (1–8).">
              {number("max_concurrent_downloads", 1, 8)}
            </Field>
            <Field label="Connections per file" hint="Each file is fetched in segments spread over the Kiwix mirrors (1–16).">
              {number("connections_per_file", 1, 16)}
            </Field>
            <Field label="Speed limit (KB/s)" hint="Shared by all downloads. 0 means no limit.">
              {number("bandwidth_limit_kbps", 0)}
            </Field>
            <Field label="Keep old versions" hint="Leave the previous file on disk after an update instead of deleting it.">
              <input type="checkbox" className="h-4 w-4" checked={form.keep_old_versions} onChange={(e) => set("keep_old_versions", e.target.checked)} />
            </Field>
          </section>
          <section className="card divide-y divide-zinc-100 px-4 dark:divide-zinc-800">
            <h2 className="py-3 font-medium">Updates</h2>
            <Field label="Check the catalog every (hours)">{number("catalog_refresh_hours", 1)}</Field>
            <Field label="Policy for new books" hint="Each book's own policy can be changed on the Updates page.">
              <select className="input w-auto" value={form.default_policy} onChange={(e) => set("default_policy", e.target.value)}>
                <option value="notify">Ask first</option>
                <option value="auto">Automatic</option>
                <option value="pinned">Pinned</option>
              </select>
            </Field>
          </section>
          <section id="ask-ai" className="card scroll-mt-16 divide-y divide-zinc-100 px-4 dark:divide-zinc-800">
            <div className="py-3">
              <h2 className="font-medium">Ask AI</h2>
              <p className="muted text-xs">
                Works with any server that speaks the OpenAI chat API, hosted or local. Questions and the article
                passages used to answer them are sent to this server.
              </p>
            </div>
            <Field group label="Provider" hint="Pick a preset, or Custom for any other OpenAI-compatible server.">
              <div className="w-full sm:w-96">
                <Combobox
                  wide
                  value={providerId}
                  options={PROVIDER_OPTIONS}
                  placeholder="Choose a provider"
                  searchPlaceholder="Search providers…"
                  className={PICKER}
                  onChange={pickProvider}
                />
              </div>
            </Field>
            <Field
              label="Server address"
              hint="The API base URL, usually ending in /v1. From Docker, a server on this machine is host.docker.internal, not localhost."
            >
              <input
                ref={urlInput}
                className="input w-full font-mono text-xs sm:w-96"
                placeholder="https://api.openai.com/v1"
                aria-label="Server address"
                value={form.ai_base_url}
                onChange={(e) => (set("ai_base_url", e.target.value), setProbe(null))}
              />
            </Field>
            <Field
              label="API key"
              hint="Leave empty for local servers that do not need one. It is stored on this server and never shown again."
            >
              <input
                type="password"
                autoComplete="off"
                className="input w-full sm:w-96"
                placeholder={provider?.local ? "Not needed" : "sk-…"}
                aria-label="API key"
                value={form.ai_api_key}
                onFocus={(e) => e.target.select()}
                onChange={(e) => (set("ai_api_key", e.target.value), setProbe(null))}
              />
            </Field>
            <Field
              group
              label="Model"
              hint={
                models.length
                  ? `${models.length} models offered by this server. You can also type a name that is not listed.`
                  : "Press “Test connection” to list the server's models, or type a model name."
              }
            >
              <div className="w-full sm:w-96">
                <Combobox
                  wide
                  allowCustom
                  value={form.ai_model}
                  options={modelOptions}
                  placeholder="Choose a model"
                  searchPlaceholder={models.length ? `Search ${models.length} models…` : "Type a model name…"}
                  className={PICKER}
                  onChange={(value) => set("ai_model", value)}
                />
              </div>
            </Field>
            <Field label="Sources per answer" hint="How many article passages are given to the model (1–12). Lower this for models with a small context window.">
              {number("ai_max_sources", 1, 12)}
            </Field>
            <div className="flex flex-wrap items-center gap-3 py-3">
              <button type="button" className="btn" disabled={!form.ai_base_url.trim()} onClick={() => testAi()}>
                Test connection
              </button>
              {probe && (
                <span className={`text-sm ${probe.ok ? "text-brand-600" : "text-red-600 dark:text-red-400"}`}>{probe.text}</span>
              )}
            </div>
          </section>
          <section className="card divide-y divide-zinc-100 px-4 dark:divide-zinc-800">
            <h2 className="py-3 font-medium">MCP server</h2>
            <div className="py-3 text-sm">
              <p className="muted">LLM apps can search and read this library at:</p>
              <code className="mt-1.5 block overflow-x-auto rounded-lg bg-zinc-100 px-3 py-2 text-xs dark:bg-zinc-800">{mcpUrl}</code>
              <p className="muted mt-2">For Claude Code:</p>
              <code className="mt-1.5 block overflow-x-auto rounded-lg bg-zinc-100 px-3 py-2 text-xs whitespace-nowrap dark:bg-zinc-800">
                claude mcp add --transport http kiwix {mcpUrl}
              </code>
            </div>
            <Field label="Let LLM apps start updates" hint="Off: MCP clients can only search and read.">
              <input type="checkbox" className="h-4 w-4" checked={form.mcp_allow_writes} onChange={(e) => set("mcp_allow_writes", e.target.checked)} />
            </Field>
          </section>
          <section className="card px-4 py-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h2 className="font-medium">Library folder</h2>
                <p className="muted text-xs">The folder is rescanned every 5 minutes; rescan now after copying files in by hand.</p>
              </div>
              <button className="btn" onClick={rescan}>
                Rescan now
              </button>
            </div>
            {!!leftovers.data?.length && (
              <div className="mt-3 border-t border-zinc-100 pt-3 dark:border-zinc-800">
                <p className="text-sm font-medium">Other files in the folder</p>
                <p className="muted mb-2 text-xs">
                  These are not served: older versions of a book, leftovers from earlier downloads, or unrelated files.
                </p>
                <ul className="space-y-1 text-xs">
                  {leftovers.data.map((file) => (
                    <li key={file.path} className="flex justify-between gap-4">
                      <span className="truncate font-mono">{file.name}</span>
                      <span className="muted whitespace-nowrap">
                        {file.superseded_by ? "older version · " : ""}
                        {bytes(file.size)}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </section>
          <div className="pointer-events-none sticky bottom-4 z-20 flex justify-end">
            <div className="card pointer-events-auto flex items-center gap-3 py-2 pr-2 pl-4 shadow-lg">
              <span className={`text-sm ${dirty ? "" : "muted"}`} role="status">
                {dirty ? "Unsaved changes" : (note ?? "All changes saved")}
              </span>
              <button className="btn btn-primary" disabled={!dirty} onClick={save}>
                Save
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
