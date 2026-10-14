import { useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Combobox, type Option } from "../Combobox";
import { useModels } from "../lib/ai";
import { api, post } from "../lib/api";
import { bytes } from "../lib/format";
import { useData, useLive } from "../lib/live";
import { SolverStatus, SolverTest, VpnStatus, VpnTest } from "../Network";
import { ErrorNote, Switch } from "../ui";
import { Field, ModelPicker, PICKER, playSample, Providers, Segmented } from "./AiSettings";
import Logs from "./Logs";
import Monitor from "./Monitor";
import Queues from "./Queues";
import { About, McpAccess, SettingsNav, TAB_OF, type TabId } from "./SettingsParts";

interface SettingsData {
  max_concurrent_downloads: number;
  connections_per_file: number;
  bandwidth_limit_kbps: number;
  catalog_refresh_hours: number;
  default_policy: string;
  keep_old_versions: boolean;
  mcp_allow_writes: boolean;
  search_history: boolean;
  max_concurrent_builds: number;
  build_workers: number;
  upload_limit_mb: number;
  build_proxy: string;
  captcha_solver_url: string;
  build_proxy_enabled: boolean;
  captcha_solver_enabled: boolean;
  vpn_control_url: string;
  vpn_control_key: string;
  vpn_control_user: string;
  vpn_control_password: string;
  vpn_change_ip: boolean;
  feed_auto_sync: boolean;
  max_concurrent_feed_syncs: number;
  feed_default_interval: number;
  feed_default_keep: number;
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
  log_level: string;
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
  const { refresh, network } = useLive();
  const loaded = useData<SettingsData>("/api/settings");
  const leftovers = useData<Leftover[]>("/api/library/leftovers");
  const [form, setForm] = useState<SettingsData | null>(null);
  const [historyCleared, setHistoryCleared] = useState(false);
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
  // The switches in the menu write straight to the server: take what they set, so that
  // saving this page does not put the old value back.
  useEffect(() => {
    if (!network) return;
    api<SettingsData>("/api/settings")
      .then((fresh) => {
        const keys = ["build_proxy_enabled", "captcha_solver_enabled", "vpn_change_ip"] as const;
        const patch = Object.fromEntries(keys.map((key) => [key, fresh[key]]));
        setSaved((current) => {
          if (!current) return current;
          const before = JSON.parse(current) as SettingsData;
          // Only where this page has not been changed since: an edit made here stands.
          setForm((form) => form && { ...form, ...Object.fromEntries(keys.filter((key) => form[key] === before[key]).map((key) => [key, fresh[key]])) });
          return JSON.stringify({ ...before, ...patch });
        });
      })
      .catch(() => {});
  }, [network]);
  // The section shown is in the address, so a link can lead straight to one.
  const anchor = useLocation().hash.slice(1);
  const navigate = useNavigate();
  const tab: TabId = TAB_OF[anchor] ?? "general";
  useEffect(() => {
    // A link to a part of a section goes to that part; a section itself starts at its top.
    const part = anchor && anchor !== tab ? document.getElementById(anchor) : null;
    if (part) part.scrollIntoView();
    else window.scrollTo(0, 0);
  }, [anchor, tab, form === null]);
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
  // Sections that change a setting themselves (a queue's limit, the log level): take the new
  // value into the form, so that Save does not put the old one back.
  const adopt = (patch: Partial<SettingsData>) => {
    setForm((current) => current && { ...current, ...patch });
    setSaved((current) => (current ? JSON.stringify({ ...JSON.parse(current), ...patch }) : current));
  };
  const keepLogLevel = async (level: string) => {
    await api("/api/settings", { method: "PUT", body: JSON.stringify({ log_level: level }) });
    adopt({ log_level: level });
  };
  const dirty = !!form && JSON.stringify(form) !== saved;

  return (
    <>
      {/* The title and Save stay in reach however far the page is scrolled. */}
      <header className="sticky top-0 z-30 -mx-4 -mt-5 mb-4 flex min-h-[3.75rem] items-center justify-between gap-3 border-b border-zinc-200 bg-zinc-50/95 px-4 py-2.5 backdrop-blur md:-mx-8 md:-mt-6 md:mb-5 md:px-8 dark:border-zinc-800 dark:bg-zinc-950/95">
        <h1 className="text-[1.7rem] leading-tight font-bold tracking-tight sm:text-2xl sm:font-semibold">Settings</h1>
        {form && (
          <div className="flex min-w-0 items-center gap-3">
            <span className={`truncate text-sm ${dirty ? "font-medium text-amber-700 dark:text-amber-400" : "muted"}`} role="status">
              {dirty ? "Unsaved changes" : (note ?? "All changes saved")}
            </span>
            <button className="btn btn-primary shrink-0" disabled={!dirty} onClick={save}>
              Save
            </button>
          </div>
        )}
      </header>
      <ErrorNote message={loaded.error ?? problem} />
      {form && (
        <div className="md:grid md:grid-cols-[14rem_minmax(0,1fr)] md:items-start md:gap-8">
          <SettingsNav tab={tab} onChange={(next) => navigate({ hash: next }, { replace: true })} />
          <div className="min-w-0 space-y-5">
            {tab === "general" && (
              <>
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
              <section className="card divide-y divide-zinc-100 px-4 dark:divide-zinc-800">
                <h2 className="py-3 font-medium">Search</h2>
                <Field label="Keep search history" hint="Remember searches made on the Search page; the history button there shows them. Turning this off keeps what is already saved until you clear it.">
                  <input type="checkbox" className="h-4 w-4" checked={form.search_history} onChange={(e) => set("search_history", e.target.checked)} />
                </Field>
                <Field label="Saved searches" hint="Remove every search remembered so far.">
                  <button
                    type="button"
                    className="btn"
                    onClick={() => api("/api/search/history", { method: "DELETE" }).then(() => setHistoryCleared(true))}
                    disabled={historyCleared}
                  >
                    {historyCleared ? "Cleared" : "Clear history"}
                  </button>
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
              </>
            )}
            {tab === "ai" && (
              <>
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
              </>
            )}
            {tab === "create" && (
              <>
              <section className="card divide-y divide-zinc-100 px-4 dark:divide-zinc-800">
                <h2 className="py-3 font-medium">Create</h2>
                <Field label="Builds at once" hint="How many ZIMs are made at the same time; the rest wait their turn.">{number("max_concurrent_builds", 1)}</Field>
                <Field label="Threads per build" hint="Pages fetched and converted side by side, and the threads that compress the ZIM (1–16).">{number("build_workers", 1)}</Field>
                <Field label="Largest upload (MB)" hint="Books and ZIM files above this size are refused.">{number("upload_limit_mb", 1)}</Field>
              </section>
              <section className="card divide-y divide-zinc-100 px-4 dark:divide-zinc-800">
                <h2 className="py-3 font-medium">Feeds</h2>
                <Field label="Sync on schedule" hint="Check every feed collection for new articles at the interval it sets. Off: collections only sync when you press Sync.">
                  <input type="checkbox" className="h-4 w-4" checked={form.feed_auto_sync} onChange={(e) => set("feed_auto_sync", e.target.checked)} />
                </Field>
                <Field label="Syncs at once" hint="Feed syncs have their own queue, apart from the builds on the Create page (1–4).">{number("max_concurrent_feed_syncs", 1, 4)}</Field>
                <Field label="Default interval (minutes)" hint="How often a new collection, or one imported from OPML, is checked.">{number("feed_default_interval", 5)}</Field>
                <Field label="Default articles to keep" hint="The newest this many stay in a new collection's ZIM. 0 keeps every article.">{number("feed_default_keep", 0)}</Field>
              </section>
              </>
            )}
            {tab === "network" && (
              <>
              <section className="card divide-y divide-zinc-100 px-4 dark:divide-zinc-800">
                <div className="py-3">
                  <h2 className="font-medium">VPN</h2>
                  <p className="muted text-xs">Send everything fetched for links and feeds through a VPN container such as Gluetun, so sites see its address and not yours. Browser captures (Zimit) do not use it.</p>
                </div>
                <Field group label="Send requests through the VPN" hint="Off: pages are fetched directly, and the addresses below are kept for later. The same switch is at the foot of the menu.">
                  <Switch checked={form.build_proxy_enabled} onChange={(value) => set("build_proxy_enabled", value)} label="Send requests through the VPN" />
                </Field>
                <Field label="Proxy address" hint="The container's HTTP proxy. For Gluetun, switch it on with HTTPPROXY=on; it listens on port 8888.">
                  <input className="input w-full font-mono text-xs sm:w-96" placeholder="http://gluetun:8888" value={form.build_proxy} onChange={(e) => set("build_proxy", e.target.value)} aria-label="Proxy address" />
                </Field>
                <Field label="Control server address" hint="Gluetun's control server, on port 8000. With it, this page and the menu show the connection's address and country, and the address can be changed.">
                  <input className="input w-full font-mono text-xs sm:w-96" placeholder="http://gluetun:8000" value={form.vpn_control_url} onChange={(e) => set("vpn_control_url", e.target.value)} aria-label="Control server address" />
                </Field>
                <Field label="Control server API key" hint="If the container was given one. Otherwise use the name and password below, or leave all three empty if it asks for none.">
                  <input type="password" autoComplete="off" className="input w-full sm:w-96" placeholder="API key" value={form.vpn_control_key} onFocus={(e) => e.target.select()} onChange={(e) => set("vpn_control_key", e.target.value)} aria-label="Control server API key" />
                </Field>
                <Field group label="Control server name and password" hint="For a container set up with basic authentication.">
                  <div className="flex w-full flex-wrap gap-2 sm:w-96 sm:flex-nowrap">
                    <input autoComplete="off" className="input min-w-0 flex-1" placeholder="Name" value={form.vpn_control_user} onChange={(e) => set("vpn_control_user", e.target.value)} aria-label="Control server user name" />
                    <input type="password" autoComplete="new-password" className="input min-w-0 flex-1" placeholder="Password" value={form.vpn_control_password} onFocus={(e) => e.target.select()} onChange={(e) => set("vpn_control_password", e.target.value)} aria-label="Control server password" />
                  </div>
                </Field>
                <Field
                  group
                  label="Change IP when a site says too many requests"
                  hint="When a site answers HTTP 429, the VPN is reconnected for another address and the page is asked for again, without the long wait. At most once a minute; everything going through the VPN waits the few seconds that takes. Needs the control server."
                >
                  <Switch checked={form.vpn_change_ip} onChange={(value) => set("vpn_change_ip", value)} label="Change IP when a site says too many requests" />
                </Field>
                <VpnTest typed={form} />
                <VpnStatus />
              </section>
              <section className="card divide-y divide-zinc-100 px-4 dark:divide-zinc-800">
                <div className="py-3">
                  <h2 className="font-medium">Captcha solver</h2>
                  <p className="muted text-xs">A FlareSolverr container. When a site answers with a browser check (Cloudflare's "Just a moment", DDoS-Guard), the page is opened there and the build carries on. It does not solve picture or checkbox captchas.</p>
                </div>
                <Field group label="Use the captcha solver" hint="Off: pages behind a browser check fail with a note. The same switch is at the foot of the menu.">
                  <Switch checked={form.captcha_solver_enabled} onChange={(value) => set("captcha_solver_enabled", value)} label="Use the captcha solver" />
                </Field>
                <Field label="Solver address" hint="FlareSolverr listens on port 8191. Run it inside the VPN container's network if the VPN is used, so both leave from the same address.">
                  <input className="input w-full font-mono text-xs sm:w-96" placeholder="http://flaresolverr:8191" value={form.captcha_solver_url} onChange={(e) => set("captcha_solver_url", e.target.value)} aria-label="Captcha solver address" />
                </Field>
                <SolverTest address={form.captcha_solver_url} />
                <SolverStatus />
              </section>
              </>
            )}
            {tab === "mcp" && (
              <>
              <McpAccess />
              <section className="card divide-y divide-zinc-100 px-4 dark:divide-zinc-800">
                <h2 className="py-3 font-medium">What LLM apps may do</h2>
                <Field label="Let LLM apps start updates" hint="Off: MCP clients can only search and read.">
                  <input type="checkbox" className="h-4 w-4" checked={form.mcp_allow_writes} onChange={(e) => set("mcp_allow_writes", e.target.checked)} />
                </Field>
              </section>
              </>
            )}
            {tab === "queues" && <Queues onSetting={adopt} />}
            {tab === "monitoring" && <Monitor />}
            {tab === "logs" && <Logs onKeepLevel={keepLogLevel} />}
            {tab === "about" && <About />}
          </div>
        </div>
      )}
    </>
  );
}
