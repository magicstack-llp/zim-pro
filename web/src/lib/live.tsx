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
  refresh: () => void;
}

const LiveContext = createContext<Live>({ downloads: [], builds: [], feeds: [], revision: 0, refresh: () => {} });

/** Keeps download progress and change notifications current over server-sent events. */
export function LiveProvider({ children }: { children: ReactNode }) {
  const [downloads, setDownloads] = useState<Download[]>([]);
  const [builds, setBuilds] = useState<Build[]>([]);
  const [feeds, setFeeds] = useState<Build[]>([]);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => {
    api<Download[]>("/api/downloads").then(setDownloads).catch(() => {});
    api<Build[]>("/api/build/jobs").then(setBuilds).catch(() => {});
    api<Build[]>("/api/feeds/runs").then(setFeeds).catch(() => {});
    setRevision((value) => value + 1);
  }, []);

  useEffect(() => {
    api<Download[]>("/api/downloads").then(setDownloads).catch(() => {});
    api<Build[]>("/api/build/jobs").then(setBuilds).catch(() => {});
    api<Build[]>("/api/feeds/runs").then(setFeeds).catch(() => {});
    const source = new EventSource("/api/events");
    source.addEventListener("builds", (event) => setBuilds(JSON.parse((event as MessageEvent).data)));
    source.addEventListener("feeds", (event) => setFeeds(JSON.parse((event as MessageEvent).data)));
    source.addEventListener("downloads", (event) => setDownloads(JSON.parse((event as MessageEvent).data)));
    const bump = () => setRevision((value) => value + 1);
    source.addEventListener("library", bump);
    source.addEventListener("catalog", bump);
    return () => source.close();
  }, []);

  return <LiveContext.Provider value={{ downloads, builds, feeds, revision, refresh }}>{children}</LiveContext.Provider>;
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
    if (path === null) return;
    const request = ++latest.current;
    setLoading(true);
    api<T>(path)
      .then((result) => {
        if (request !== latest.current) return;
        setData(result);
        setError(null);
      })
      .catch((reason) => request === latest.current && setError(String(reason.message ?? reason)))
      .finally(() => request === latest.current && setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, revision, nonce, ...deps]);

  return { data, error, loading, reload: () => setNonce((value) => value + 1) };
}
