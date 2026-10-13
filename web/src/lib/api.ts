const TOKEN_KEY = "kiwix-admin-token";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export function authHeaders(): Record<string, string> {
  const token = localStorage.getItem(TOKEN_KEY);
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export async function api<T = any>(path: string, init: RequestInit = {}, retry = true): Promise<T> {
  const headers = new Headers(init.headers);
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...init, headers });
  if (response.status === 401 && retry) {
    const entered = window.prompt("This server needs the admin token to make changes:");
    if (entered) {
      localStorage.setItem(TOKEN_KEY, entered.trim());
      return api(path, init, false);
    }
  }
  if (!response.ok) {
    let message = response.statusText;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, message);
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
  report: { pages?: number; files?: number; searchable?: boolean; downloaded?: number; reused_from_cache?: number; from_sitemaps?: number; from_feeds?: number; rss?: boolean; slowed?: number; solved?: number; new?: number; found?: number; articles?: number; dropped?: number; unchanged?: boolean };
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
