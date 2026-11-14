const TOKEN_KEY = "kiwix-admin-token";
const DEFAULT_TIMEOUT_MS = 30_000;
const MUTATION_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

let promptDeclined = false;

export function resetAuthPromptState(): void {
  promptDeclined = false;
}

function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* storage blocked */
  }
}

function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage blocked */
  }
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export function formatErrorDetail(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string" && detail) return detail;
    if (Array.isArray(detail)) {
      const parts = detail
        .map((item) => {
          if (typeof item === "string") return item;
          if (item && typeof item === "object") {
            const entry = item as { loc?: unknown; msg?: unknown };
            const loc = Array.isArray(entry.loc) ? entry.loc.slice(1).join(".") : "";
            const msg = typeof entry.msg === "string" ? entry.msg : JSON.stringify(item);
            return loc ? `${loc}: ${msg}` : msg;
          }
          return "";
        })
        .filter(Boolean);
      if (parts.length) return parts.join("; ");
    }
  }
  return fallback || "Request failed";
}

export async function parseResponseError(response: Response): Promise<string> {
  let message = response.statusText || `HTTP ${response.status}`;
  try {
    const body = await response.json();
    message = formatErrorDetail(body, message);
  } catch {
    /* not JSON */
  }
  return message;
}

export interface ApiOptions extends RequestInit {
  timeoutMs?: number;
}

export async function api<T = any>(path: string, init: ApiOptions = {}, retry = true): Promise<T> {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, signal: callerSignal, ...rest } = init;
  const headers = new Headers(rest.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (rest.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");

  const controller = new AbortController();
  const timer = timeoutMs > 0 ? setTimeout(() => controller.abort(new DOMException("Request timed out", "TimeoutError")), timeoutMs) : undefined;
  const onCallerAbort = () => controller.abort(callerSignal?.reason);
  if (callerSignal) {
    if (callerSignal.aborted) controller.abort(callerSignal.reason);
    else callerSignal.addEventListener("abort", onCallerAbort, { once: true });
  }

  let response: Response;
  try {
    response = await fetch(path, { ...rest, headers, signal: controller.signal });
  } finally {
    if (timer !== undefined) clearTimeout(timer);
    callerSignal?.removeEventListener("abort", onCallerAbort);
  }

  const method = (rest.method ?? "GET").toUpperCase();
  if (response.status === 401) {
    if (token) clearToken();
    if (retry && MUTATION_METHODS.has(method) && !promptDeclined) {
      const entered = window.prompt("This server needs the admin token to make changes:");
      if (entered && entered.trim()) {
        promptDeclined = false;
        setToken(entered.trim());
        return api(path, init, false);
      }
      promptDeclined = true;
    }
  }
  if (!response.ok) {
    throw new ApiError(response.status, await parseResponseError(response));
  }
  return response.json();
}

export const post = <T = any>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

/** One run of making a ZIM, from a link or an uploaded book. */
export interface Build {
  id: number;
  kind: "link" | "upload" | "feed";
  source_id: number;
  title: string;
  state: "queued" | "processing" | "paused" | "finished" | "failed" | "cancelled";
  phase: string;
  /** Why nothing is moving right now, such as a site asking for a pause. */
  detail?: string;
  done: number;
  total: number;
  failed: number;
  skipped: number;
  size: number;
  error: string | null;
  report: { pages?: number; files?: number; searchable?: boolean; downloaded?: number; reused_from_cache?: number; from_sitemaps?: number; from_feeds?: number; rss?: boolean; slowed?: number; solved?: number; address_changes?: number; new?: number; found?: number; articles?: number; dropped?: number; unchanged?: boolean };
  params?: { rebuild?: boolean; rss?: boolean; trigger?: "manual" | "schedule" };
  book_id: string;
  created: number;
  started: number | null;
  finished: number | null;
  log?: { kind: "failed" | "skipped" | "note"; target: string; why: string }[];
}

export interface Book {
  id: string;
  filename: string;
  size: number;
  name: string;
  flavour: string;
  title: string;
  description: string;
  language: string;
  date: string;
  uuid: string;
  creator: string;
  publisher: string;
  tags: string;
  category: string;
  article_count: number;
  media_count: number;
  has_ftindex: number;
  has_illustration: number;
  policy: "auto" | "notify" | "pinned";
  error: string | null;
}

export interface CatalogEntry {
  uuid: string;
  stem: string;
  name: string;
  flavour: string;
  title: string;
  summary: string;
  language: string;
  category: string;
  tags: string;
  author: string;
  article_count: number;
  media_count: number;
  issued: string;
  size: number;
  illustration: string;
  local: "current" | "outdated" | null;
  book_id: string | null;
  download_state: string | null;
}

export interface Download {
  id: number;
  catalog_uuid: string;
  book_id: string;
  title: string;
  filename: string;
  size: number;
  bytes_done: number;
  state: "queued" | "downloading" | "verifying" | "paused" | "done" | "failed" | "cancelled";
  replaces: string | null;
  from_date: string;
  to_date: string;
  error: string | null;
  speed: number;
  connections: number;
  eta: number | null;
  updated: number;
}

export interface UpdateItem {
  book: Book;
  status: "update" | "current" | "unpublished";
  latest: CatalogEntry | null;
  download: { id: number; state: Download["state"]; bytes_done: number; size: number; error: string | null } | null;
}

export interface Status {
  books: number;
  library_bytes: number;
  disk_free: number;
  disk_total: number;
  catalog_entries: number;
  catalog_refreshed: number | null;
  catalog_error: string | null;
  ai_configured: boolean;
  ai_provider: string;
  ai_model: string;
  web_mode: "off" | "api" | "model";
  tts_mode: "browser" | "provider" | "off";
}

export const contentUrl = (bookId: string, path = "") =>
  `/content/${encodeURIComponent(bookId)}/${path.split("/").map(encodeURIComponent).join("/")}`;

export const markdownUrl = (bookId: string, path = "") =>
  `/markdown/${encodeURIComponent(bookId)}/${path.split("/").map(encodeURIComponent).join("/")}`;

export const readUrl = (bookId: string, path = "") =>
  `/read/${encodeURIComponent(bookId)}/${path.split("/").map(encodeURIComponent).join("/")}`;
