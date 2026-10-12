import { useCallback, useEffect, useState } from "react";
import type { Option } from "../Combobox";
import { api } from "./api";

export interface ProviderInfo {
  id: string;
  name: string;
  kind: string;
  base_url: string;
  has_key: boolean;
  /** Off: kept, but its models are not offered anywhere. */
  enabled: boolean;
  /** Works with no account or key, within a rate limit. */
  free?: boolean;
  /** Its models can search the web themselves. */
  web: boolean;
}
export interface ModelGroup {
  provider: ProviderInfo;
  models: string[];
  error: string | null;
}

export interface Preset {
  kind: string;
  name: string;
  url: string;
  about: string;
  local?: boolean;
  /** Needs no account or key. `note` says what to expect instead. */
  free?: boolean;
  note?: string;
  /** Where the user gets an API key, shown as a link next to the key field. */
  keys?: string;
}
export const PRESETS: Preset[] = [
  { kind: "openai", name: "OpenAI", url: "https://api.openai.com/v1", about: "GPT models, web search, speech", keys: "https://platform.openai.com/api-keys" },
  { kind: "gemini", name: "Google Gemini", url: "https://generativelanguage.googleapis.com/v1beta/openai", about: "Gemini and Gemma, Google Search, speech", keys: "https://aistudio.google.com/apikey" },
  { kind: "perplexity", name: "Perplexity", url: "https://api.perplexity.ai", about: "Sonar models that search the web", keys: "https://www.perplexity.ai/settings/api" },
  { kind: "openrouter", name: "OpenRouter", url: "https://openrouter.ai/api/v1", about: "Hundreds of models, one key", keys: "https://openrouter.ai/keys" },
  { kind: "groq", name: "Groq", url: "https://api.groq.com/openai/v1", about: "Fast open models, speech", keys: "https://console.groq.com/keys" },
  {
    kind: "ovh",
    name: "OVHcloud AI Endpoints",
    url: "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
    about: "Llama, Qwen, Mistral, gpt-oss · no key",
    free: true,
    note: "No account or key needed. Open models such as Llama 3.3 70B, Qwen and gpt-oss, hosted in Europe. Without a key it allows about two questions a minute per model, so switch model if you hit the limit.",
  },
  {
    kind: "kilo",
    name: "Kilo Gateway (free models)",
    url: "https://api.kilo.ai/api/gateway/v1",
    about: "A rotating set of free models · no key",
    free: true,
    note: "No account or key needed. Only the models marked free are offered, the set changes over time, and it allows about 200 requests an hour. Kilo says prompts sent to its free models may be logged and used for training, so do not use it for anything private.",
  },
  { kind: "ollama", name: "Ollama", url: "http://host.docker.internal:11434/v1", about: "Runs on this machine", local: true },
  { kind: "lmstudio", name: "LM Studio", url: "http://host.docker.internal:1234/v1", about: "Runs on this machine", local: true },
  { kind: "llamacpp", name: "llama.cpp", url: "http://host.docker.internal:8080/v1", about: "Runs on this machine", local: true },
];
export const KIND_NAMES: Record<string, string> = Object.fromEntries([
  ...PRESETS.map((p) => [p.kind, p.name]),
  ["custom", "OpenAI-compatible"],
]);

/** A model is addressed as "<provider id>::<model name>". */
export const modelKey = (provider: string, model: string) => (provider && model ? `${provider}::${model}` : "");
export function splitModelKey(key: string): [string, string] {
  const at = key.indexOf("::");
  return at < 0 ? ["", ""] : [key.slice(0, at), key.slice(at + 2)];
}
export const shortModel = (model: string) => model.replace(/^models\//, "");

export function modelOptions(groups: ModelGroup[], only?: (provider: ProviderInfo) => boolean): Option[] {
  return groups
    .filter((group) => !only || only(group.provider))
    .flatMap((group) =>
      group.models.map((model) => ({
        value: modelKey(group.provider.id, model),
        label: shortModel(model),
        group: group.provider.name,
      })),
    );
}

/** Every provider with its models. `reload(true)` asks the providers again instead of the cache. */
export function useModels() {
  const [groups, setGroups] = useState<ModelGroup[] | null>(null);
  const reload = useCallback((refresh = false) => {
    api<ModelGroup[]>(`/api/ai/models${refresh ? "?refresh=true" : ""}`)
      .then(setGroups)
      .catch(() => setGroups([]));
  }, []);
  useEffect(() => reload(), [reload]);
  return { groups, reload };
}
