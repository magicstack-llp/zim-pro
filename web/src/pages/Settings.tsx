import { useEffect, useMemo, useState } from "react";
import { Combobox, type Option } from "../Combobox";
import { useModels } from "../lib/ai";
import { api, post } from "../lib/api";
import { bytes } from "../lib/format";
import { useData, useLive } from "../lib/live";
import { ErrorNote, PageHeader } from "../ui";
import { Field, ModelPicker, PICKER, playSample, Providers, Segmented } from "./AiSettings";

interface SettingsData {
  max_concurrent_downloads: number;
  connections_per_file: number;
  bandwidth_limit_kbps: number;
  catalog_refresh_hours: number;
  default_policy: string;
  keep_old_versions: boolean;
  mcp_allow_writes: boolean;
  ai_provider: string;
  ai_model: string;
  ai_max_sources: number;
  ai_instructions: string;
  web_mode: "off" | "api" | "model";
  web_service: string;
  web_api_key: string;
  web_api_url: string;
  web_provider: string;
  web_model: string;
  tts_mode: "browser" | "provider" | "off";
  tts_provider: string;
  tts_model: string;
  tts_voice: string;
}

const SEARCH_SERVICES: Option[] = [
  { value: "tavily", label: "Tavily", description: "Search API made for AI answers · needs a key", badge: "T" },
  { value: "brave", label: "Brave Search", description: "Independent index · needs a key", badge: "B" },
  { value: "serper", label: "Serper", description: "Google results · needs a key", badge: "G" },
  { value: "searxng", label: "SearXNG", description: "Your own search server · no key", badge: "S" },
];
const WEB_MODEL_HINTS: Record<string, string> = {
  perplexity: "Perplexity: any Sonar model searches on its own.",
  openrouter: "OpenRouter: any model works; the web add-on is billed per search.",
  openai: "OpenAI: pick a model with “search” in its name.",
  gemini: "Google: pick a Gemini model. Gemma models cannot search.",
};
const TTS_DEFAULTS: Record<string, [string, string]> = {
  openai: ["gpt-4o-mini-tts", "alloy"],
  gemini: ["gemini-2.5-flash-preview-tts", "Kore"],
  groq: ["playai-tts", "Fritz-PlayAI"],
};

interface Leftover {
  path: string;
  name: string;
  size: number;
  superseded_by?: string;
}

const onlyWeb = (provider: { web: boolean }) => provider.web;

export default function Settings() {
  const { refresh } = useLive();
  const loaded = useData<SettingsData>("/api/settings");
  const leftovers = useData<Leftover[]>("/api/library/leftovers");
  const [form, setForm] = useState<SettingsData | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState("");
  const [voiceTest, setVoiceTest] = useState<{ ok: boolean; text: string } | null>(null);
  const { groups, reload: reloadModels } = useModels();
  const providers = useMemo(() => (groups ?? []).map((g) => g.provider), [groups]);
  const webProviders = providers.filter((p) => p.web);
  // A provider was added, changed or removed: refresh the lists and pick up the defaults the
  // server may have adjusted, without discarding edits to other fields.
  const providersChanged = async () => {
    reloadModels(true);
    refresh();
    const fresh = await api<SettingsData>("/api/settings");
    const keys = ["ai_provider", "ai_model", "web_provider", "tts_provider"] as const;
    const patch = Object.fromEntries(keys.map((key) => [key, fresh[key]]));
    setForm((current) => current && { ...current, ...patch });
    setSaved((current) => (current ? JSON.stringify({ ...JSON.parse(current), ...patch }) : current));
  };
  const testVoice = async () => {
    if (!form) return;
    setVoiceTest({ ok: true, text: "Generating…" });
    try {
      await playSample(form.tts_provider, form.tts_model, form.tts_voice);
      setVoiceTest({ ok: true, text: "Playing" });
    } catch (reason) {
      setVoiceTest({ ok: false, text: String((reason as Error).message) });
    }
  };
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
      inputMode="numeric"
      className="input w-full sm:w-28"
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
          <Providers groups={groups} onChanged={providersChanged} />

          <section className="card divide-y divide-zinc-100 px-4 dark:divide-zinc-800">
            <h2 className="py-3 font-medium">Ask AI</h2>
            <Field group label="Default model" hint="Used for new chats. You can switch model for any message in the chat box.">
              <ModelPicker groups={groups} provider={form.ai_provider} model={form.ai_model} onChange={(p, m) => (set("ai_provider", p), set("ai_model", m))} />
            </Field>
            <Field label="Sources per answer" hint="How many article passages are given to the model (1–12). Lower this for models with a small context window.">
              {number("ai_max_sources", 1, 12)}
            </Field>
            <Field
              label="Your instructions"
              hint="Added to every question, on top of the built-in formatting rules. For example: “Answer in Hindi”, “Keep answers under 150 words”, “Explain like I'm new to this”."
            >
              <textarea
                className="input min-h-20 w-full sm:w-96"
                rows={3}
                maxLength={2000}
                placeholder="Optional"
                value={form.ai_instructions}
                onChange={(e) => set("ai_instructions", e.target.value)}
                aria-label="Your instructions"
              />
            </Field>
          </section>

          <section id="web-search" className="card scroll-mt-16 divide-y divide-zinc-100 px-4 dark:divide-zinc-800">
            <div className="flex flex-wrap items-center justify-between gap-3 py-3">
              <div className="min-w-0 flex-1 basis-64">
                <h2 className="font-medium">Web search</h2>
                <p className="muted text-xs">
                  Used automatically when your library has no good match for a question, and whenever you turn on the globe
                  button in the chat box. Your question is sent to the service you choose.
                </p>
              </div>
              <Segmented
                label="Web search"
                value={form.web_mode}
                onChange={(value) => set("web_mode", value)}
                options={[
                  { value: "off", label: "Off" },
                  { value: "api", label: "Search service" },
                  { value: "model", label: "Searching model" },
                ]}
              />
            </div>
            {form.web_mode === "api" && (
              <>
                <Field group label="Search service" hint="Its results are added to the sources, and your chat model writes the answer.">
                  <div className="w-full sm:w-96">
                    <Combobox wide value={form.web_service} options={SEARCH_SERVICES} placeholder="Choose a service" className={PICKER} onChange={(value) => set("web_service", value)} />
                  </div>
                </Field>
                {form.web_service === "searxng" ? (
                  <Field label="SearXNG address" hint="The JSON format must be enabled in its settings (search.formats).">
                    <input className="input w-full font-mono text-xs sm:w-96" placeholder="http://host.docker.internal:8888" value={form.web_api_url} onChange={(e) => set("web_api_url", e.target.value)} aria-label="SearXNG address" />
                  </Field>
                ) : (
                  <Field label="API key" hint="Stored on this server and never shown again.">
                    <input type="password" autoComplete="off" className="input w-full sm:w-96" placeholder="Paste the key" value={form.web_api_key} onFocus={(e) => e.target.select()} onChange={(e) => set("web_api_key", e.target.value)} aria-label="Search API key" />
                  </Field>
                )}
              </>
            )}
            {form.web_mode === "model" && (
              <Field
                group
                label="Searching model"
                hint={
                  webProviders.length
                    ? (WEB_MODEL_HINTS[providers.find((p) => p.id === form.web_provider)?.kind ?? ""] ??
                      "A model that searches the internet itself and answers with links to the pages it used.")
                    : "Add a Perplexity, OpenRouter, OpenAI or Google Gemini provider above first."
                }
              >
                <ModelPicker groups={groups} provider={form.web_provider} model={form.web_model} only={onlyWeb} onChange={(p, m) => (set("web_provider", p), set("web_model", m))} />
              </Field>
            )}
          </section>

          <section id="read-aloud" className="card scroll-mt-16 divide-y divide-zinc-100 px-4 dark:divide-zinc-800">
            <div className="flex flex-wrap items-center justify-between gap-3 py-3">
              <div className="min-w-0 flex-1 basis-64">
                <h2 className="font-medium">Read aloud</h2>
                <p className="muted text-xs">
                  Adds a listen button to each answer. The browser voice is free and works offline; a provider voice sounds
                  more natural but sends the answer text to that provider.
                </p>
              </div>
              <Segmented
                label="Read aloud"
                value={form.tts_mode}
                onChange={(value) => set("tts_mode", value)}
                options={[
                  { value: "browser", label: "Browser voice" },
                  { value: "provider", label: "Provider voice" },
                  { value: "off", label: "Off" },
                ]}
              />
            </div>
            {form.tts_mode === "provider" && (
              <>
                <Field group label="Provider" hint="Needs a speech endpoint: OpenAI, Google Gemini, Groq, or a local server with /audio/speech.">
                  <div className="w-full sm:w-96">
                    <Combobox
                      wide
                      value={form.tts_provider}
                      options={providers.map((p) => ({ value: p.id, label: p.name, badge: p.name.charAt(0) }))}
                      placeholder="Choose a provider"
                      className={PICKER}
                      onChange={(value) => set("tts_provider", value)}
                    />
                  </div>
                </Field>
                <Field label="Speech model" hint="Leave empty for the provider's usual one.">
                  <input className="input w-full sm:w-96" placeholder={TTS_DEFAULTS[(providers.find((p) => p.id === form.tts_provider)?.kind ?? "")]?.[0] ?? "tts-1"} value={form.tts_model} onChange={(e) => set("tts_model", e.target.value)} aria-label="Speech model" />
                </Field>
                <Field label="Voice" hint="Leave empty for the default voice.">
                  <input className="input w-full sm:w-96" placeholder={TTS_DEFAULTS[(providers.find((p) => p.id === form.tts_provider)?.kind ?? "")]?.[1] ?? "alloy"} value={form.tts_voice} onChange={(e) => set("tts_voice", e.target.value)} aria-label="Voice" />
                </Field>
                <div className="flex flex-wrap items-center gap-3 py-3">
                  <button type="button" className="btn" disabled={!form.tts_provider} onClick={testVoice}>
                    Test voice
                  </button>
                  {voiceTest && <span className={`text-sm ${voiceTest.ok ? "text-brand-600" : "text-red-600 dark:text-red-400"}`}>{voiceTest.text}</span>}
                </div>
              </>
            )}
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
          <div
            className={`pointer-events-none sticky bottom-[calc(4.5rem+env(safe-area-inset-bottom))] z-20 flex justify-end kb:bottom-2 md:bottom-4 ${
              dirty || note ? "" : "max-md:hidden"
            }`}
          >
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
