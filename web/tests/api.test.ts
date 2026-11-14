import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { api, ApiError, authHeaders, formatErrorDetail, resetAuthPromptState } from "../src/lib/api.ts";

const origFetch = globalThis.fetch;
const origWindow = (globalThis as { window?: unknown }).window;
const origStorage = (globalThis as { localStorage?: unknown }).localStorage;

function restoreGlobals() {
  globalThis.fetch = origFetch;
  if (origWindow === undefined) delete (globalThis as { window?: unknown }).window;
  else (globalThis as { window?: unknown }).window = origWindow;
  if (origStorage === undefined) delete (globalThis as { localStorage?: unknown }).localStorage;
  else (globalThis as { localStorage?: unknown }).localStorage = origStorage;
  resetAuthPromptState();
}

afterEach(restoreGlobals);

function makeStorage(initial: Record<string, string> = {}) {
  const store = new Map(Object.entries(initial));
  return {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => {
      store.set(k, v);
    },
    removeItem: (k: string) => {
      store.delete(k);
    },
  };
}

test("GET 401 never prompts, and POST 401 prompts once and remembers decline", async () => {
  (globalThis as { localStorage?: unknown }).localStorage = makeStorage();
  let prompts = 0;
  (globalThis as { window?: unknown }).window = {
    prompt: () => {
      prompts += 1;
      return null;
    },
  };
  globalThis.fetch = (async () =>
    new Response(JSON.stringify({ detail: "Admin token required" }), { status: 401, statusText: "Unauthorized" })) as typeof fetch;

  await assert.rejects(() => api("/api/logs"), (err: unknown) => err instanceof ApiError && err.status === 401);
  assert.equal(prompts, 0);

  await assert.rejects(() => api("/api/settings", { method: "PUT", body: "{}" }), (err: unknown) => err instanceof ApiError && err.status === 401);
  assert.equal(prompts, 1);

  // Declined state prevents repeated prompts on subsequent mutations.
  await assert.rejects(() => api("/api/settings", { method: "PUT", body: "{}" }), (err: unknown) => err instanceof ApiError && err.status === 401);
  assert.equal(prompts, 1);
});

test("wrong token is cleared on 401 and second 401 after prompt does not re-prompt", async () => {
  const storage = makeStorage({ "kiwix-admin-token": "bad-token" });
  (globalThis as { localStorage?: unknown }).localStorage = storage;
  let prompts = 0;
  (globalThis as { window?: unknown }).window = {
    prompt: () => {
      prompts += 1;
      return "still-bad";
    },
  };
  globalThis.fetch = (async () =>
    new Response(JSON.stringify({ detail: "Invalid token" }), { status: 401, statusText: "Unauthorized" })) as typeof fetch;

  await assert.rejects(() => api("/api/build/links", { method: "POST", body: "{}" }), (err: unknown) => err instanceof ApiError && err.status === 401);
  assert.equal(prompts, 1);
  assert.equal(storage.getItem("kiwix-admin-token"), null);
});

test("formatErrorDetail formats FastAPI 422 arrays and passes through strings", () => {
  assert.equal(
    formatErrorDetail({ detail: [{ loc: ["body", "max_pages"], msg: "Input should be a valid integer", type: "int_parsing" }] }, "Unprocessable Entity"),
    "max_pages: Input should be a valid integer",
  );
  assert.equal(formatErrorDetail({ detail: "Already exists" }, "Conflict"), "Already exists");
  assert.equal(formatErrorDetail({}, "Bad Gateway"), "Bad Gateway");
});

test("api() rejects on timeoutMs and respects caller AbortSignal", async () => {
  (globalThis as { localStorage?: unknown }).localStorage = makeStorage();
  globalThis.fetch = ((_url: string, init?: RequestInit) =>
    new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(init.signal?.reason ?? new DOMException("Aborted", "AbortError")));
    })) as unknown as typeof fetch;

  await assert.rejects(() => api("/api/slow", { timeoutMs: 25 }), (err: unknown) => (err as { name?: string }).name === "TimeoutError");

  const controller = new AbortController();
  setTimeout(() => controller.abort(new DOMException("Cancelled", "AbortError")), 15);
  await assert.rejects(() => api("/api/slow", { signal: controller.signal, timeoutMs: 5000 }), (err: unknown) => (err as { name?: string }).name === "AbortError");
});

test("api() and authHeaders() handle throwing localStorage gracefully", async () => {
  (globalThis as { localStorage?: unknown }).localStorage = {
    getItem: () => {
      throw new Error("SecurityError");
    },
    setItem: () => {
      throw new Error("SecurityError");
    },
    removeItem: () => {
      throw new Error("SecurityError");
    },
  };
  assert.deepEqual(authHeaders(), {});

  let seenAuth: string | null = null;
  globalThis.fetch = (async (_url: string, init?: RequestInit) => {
    seenAuth = new Headers(init?.headers).get("Authorization");
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  }) as typeof fetch;

  const res = await api<{ ok: boolean }>("/api/status");
  assert.deepEqual(res, { ok: true });
  assert.equal(seenAuth, null);
});
