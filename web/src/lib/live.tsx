import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api, type Build, type Download } from "./api";

interface Live {
  downloads: Download[];
  /** ZIMs being made, newest first. */
  builds: Build[];
  /** Feed syncs, newest first: a queue of their own. */
  feeds: Build[];
  /** Bumps whenever the library or catalog changes on the server. */
  revision: number;
  /** Bumps whenever the VPN or the captcha solver changes: switched, or a new address. */
  network: number;
  /** True when the live event stream is disconnected. */
  offline: boolean;
  refresh: () => void;
}

const LiveContext = createContext<Live>({ downloads: [], builds: [], feeds: [], revision: 0, network: 0, offline: false, refresh: () => {} });

function safeParse<T>(raw: string): T | null {
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

/** Keeps download progress and change notifications current over server-sent events. */
export function LiveProvider({ children }: { children: ReactNode }) {
  const [downloads, setDownloads] = useState<Download[]>([]);
  const [builds, setBuilds] = useState<Build[]>([]);
  const [feeds, setFeeds] = useState<Build[]>([]);
  const [revision, setRevision] = useState(0);
  const [network, setNetwork] = useState(0);
  const [offline, setOffline] = useState(false);
  const hadError = useRef(false);

  const syncLists = useCallback((signal?: AbortSignal) => {
    api<Download[]>("/api/downloads", { signal }).then(setDownloads).catch(() => {});
    api<Build[]>("/api/build/jobs", { signal }).then(setBuilds).catch(() => {});
    api<Build[]>("/api/feeds/runs", { signal }).then(setFeeds).catch(() => {});
  }, []);

  const refresh = useCallback(() => {
    syncLists();
    setRevision((value) => value + 1);
  }, [syncLists]);

  useEffect(() => {
    const controller = new AbortController();
    syncLists(controller.signal);
    const source = new EventSource("/api/events");
    source.onopen = () => {
      setOffline(false);
      if (hadError.current) {
        hadError.current = false;
        refresh();
      }
    };
    source.onerror = () => {
      hadError.current = true;
      setOffline(true);
    };
    source.addEventListener("builds", (event) => {
      const parsed = safeParse<Build[]>((event as MessageEvent).data);
      if (parsed) setBuilds(parsed);
    });
    source.addEventListener("feeds", (event) => {
      const parsed = safeParse<Build[]>((event as MessageEvent).data);
      if (parsed) setFeeds(parsed);
    });
    source.addEventListener("downloads", (event) => {
      const parsed = safeParse<Download[]>((event as MessageEvent).data);
      if (parsed) setDownloads(parsed);
    });
    const bump = () => setRevision((value) => value + 1);
    source.addEventListener("library", bump);
    source.addEventListener("catalog", bump);
    source.addEventListener("network", () => setNetwork((value) => value + 1));
    return () => {
      controller.abort();
      source.close();
    };
  }, [refresh, syncLists]);

  return <LiveContext.Provider value={{ downloads, builds, feeds, revision, network, offline, refresh }}>{children}</LiveContext.Provider>;
}

export const useLive = () => useContext(LiveContext);

/** Fetch JSON, refetching when `deps` change or the server reports a change. */
export function useData<T>(path: string | null, deps: unknown[] = []) {
  const { revision } = useLive();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const latest = useRef(0);

  useEffect(() => {
    if (path === null) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    const request = ++latest.current;
    setLoading(true);
    api<T>(path, { signal: controller.signal })
      .then((result) => {
        if (controller.signal.aborted || request !== latest.current) return;
        setData(result);
        setError(null);
      })
      .catch((reason) => {
        if (controller.signal.aborted || request !== latest.current) return;
        setError(String((reason as Error).message ?? reason));
      })
      .finally(() => {
        if (controller.signal.aborted || request !== latest.current) return;
        setLoading(false);
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, revision, nonce, ...deps]);

  return { data, error, loading, reload: () => setNonce((value) => value + 1) };
}
