import { useEffect, useState } from "react";
import { Navigate, NavLink, Outlet, Route, Routes, useLocation } from "react-router-dom";
import type { UpdateItem } from "./lib/api";
import { ThemePicker } from "./ThemePicker";
import { useData, useLive } from "./lib/live";
import Ask from "./pages/Ask";
import Catalog from "./pages/Catalog";
import Downloads from "./pages/Downloads";
import Library from "./pages/Library";
import Reader from "./pages/Reader";
import Search from "./pages/Search";
import Settings from "./pages/Settings";
import Updates from "./pages/Updates";

const ICONS = {
  ask: "M10 3l1.6 4.4L16 9l-4.4 1.6L10 15l-1.6-4.4L4 9l4.4-1.6zM15.5 13.5l.6 1.4 1.4.6-1.4.6-.6 1.4-.6-1.4-1.4-.6 1.4-.6z",
  library: "M4 4.5h5a2 2 0 0 1 2 2V16a2 2 0 0 0-2-2H4zM16 4.5h-3a2 2 0 0 0-2 2V16a2 2 0 0 1 2-2h3z",
  search: "M9 15A6 6 0 1 0 9 3a6 6 0 0 0 0 12zM13.5 13.5 17 17",
  updates: "M15.5 8A6 6 0 0 0 4.6 7M4.5 12a6 6 0 0 0 10.9 1M15.5 4v4h-4M4.5 16v-4h4",
  downloads: "M10 3.5v9M6 9l4 4 4-4M4 16.5h12",
  catalog: "M3.5 5.5h13v10h-13zM3.5 9h13M7.5 5.5v10",
  settings: "M4 6h7M15 6h1M4 14h1M9 14h7M13 4v4M7 12v4",
  more: "M5 10h.01M10 10h.01M15 10h.01",
};

function Icon({ name, className = "h-5 w-5" }: { name: keyof typeof ICONS; className?: string }) {
  return (
    <svg viewBox="0 0 20 20" className={className} fill="none" stroke="currentColor" strokeWidth={name === "more" ? 2.6 : 1.7} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d={ICONS[name]} />
    </svg>
  );
}

function Badge({ count }: { count?: number }) {
  if (!count) return null;
  return <span className="bg-brand-600 rounded-full px-1.5 text-[11px] leading-[1.15rem] font-semibold text-white">{count > 999 ? "999+" : count}</span>;
}

function Shell() {
  const { downloads } = useLive();
  const { pathname } = useLocation();
  const updates = useData<{ update_count: number; items: UpdateItem[] }>("/api/updates");
  const active = downloads.filter((d) => ["queued", "downloading", "verifying"].includes(d.state)).length;
  const [more, setMore] = useState(false);
  useEffect(() => setMore(false), [pathname]);
  // Ask AI is an app-style screen that manages its own panels and scrolling.
  const fullBleed = pathname.startsWith("/ask");

  const links = [
    { to: "/ask", label: "Ask AI", short: "Ask", icon: "ask" },
    { to: "/search", label: "Search", short: "Search", icon: "search" },
    { to: "/library", label: "Library", short: "Library", icon: "library" },
    { to: "/updates", label: "Updates", short: "Updates", icon: "updates", badge: updates.data?.update_count },
    { to: "/downloads", label: "Downloads", short: "Downloads", icon: "downloads", badge: active },
    { to: "/catalog", label: "Catalog", short: "Catalog", icon: "catalog" },
    { to: "/settings", label: "Settings", short: "Settings", icon: "settings" },
  ] as const;
  const tabs = links.slice(0, 4);
  const overflow = links.slice(4);
  const inOverflow = overflow.some((link) => pathname.startsWith(link.to));
  const tab = (isActive: boolean) =>
    `relative flex min-w-0 flex-1 flex-col items-center justify-center gap-0.5 text-[11px] font-medium transition ${
      isActive ? "text-brand-700 dark:text-brand-400" : "text-zinc-500 dark:text-zinc-400"
    }`;

  return (
    <div className="flex min-h-full flex-col md:flex-row">
      {/* Desktop: a sidebar. */}
      <nav className="sticky top-0 z-20 hidden h-screen w-52 shrink-0 flex-col border-r border-zinc-200 bg-white/90 px-3 py-5 md:flex dark:border-zinc-800 dark:bg-zinc-900/90" aria-label="Main">
        <div className="mb-5 flex items-center gap-2 px-1">
          <img src="/icon-192.png" alt="" className="h-8 w-8 rounded-lg ring-1 ring-zinc-200 dark:ring-0" />
          <span className="font-semibold">Zim Pro</span>
        </div>
        {links.map((link) => (
          <NavLink
            key={link.to}
            to={link.to}
           
            className={({ isActive }) =>
              `mb-0.5 flex items-center gap-2.5 rounded-lg px-3 py-1.5 text-sm font-medium whitespace-nowrap transition ${
                isActive
                  ? "bg-brand-50 text-brand-700 dark:bg-brand-900/50 dark:text-brand-100"
                  : "text-zinc-600 hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-800"
              }`
            }
          >
            <Icon name={link.icon} className="h-4.5 w-4.5 shrink-0" />
            <span className="flex-1">{link.label}</span>
            <Badge count={"badge" in link ? link.badge : undefined} />
          </NavLink>
        ))}
        <div className="mt-auto pt-3">
          <ThemePicker />
        </div>
      </nav>

      {fullBleed ? (
        <main className="min-w-0 flex-1">
          <Outlet />
        </main>
      ) : (
        <main className="min-w-0 flex-1 px-4 pt-5 pb-[calc(5rem+env(safe-area-inset-bottom))] kb:pb-4 md:px-8 md:py-6">
          <div className="mx-auto max-w-6xl">
            <Outlet />
          </div>
        </main>
      )}

      {/* Phones: a tab bar at the bottom, where thumbs reach. */}
      {more && (
        <div className="fixed inset-0 z-40 md:hidden" role="dialog" aria-label="More pages">
          <button className="absolute inset-0 bg-black/40 backdrop-blur-[2px]" aria-label="Close" onClick={() => setMore(false)} />
          <div className="rise absolute inset-x-0 bottom-0 rounded-t-3xl bg-white px-3 pt-2 pb-[calc(4.5rem+env(safe-area-inset-bottom))] shadow-2xl dark:bg-zinc-900">
            <div className="mx-auto mb-2 h-1 w-10 rounded-full bg-zinc-300 dark:bg-zinc-700" />
            {overflow.map((link) => (
              <NavLink
                key={link.to}
                to={link.to}
                className={({ isActive }) =>
                  `flex items-center gap-3.5 rounded-2xl px-4 py-3.5 text-base font-medium ${
                    isActive ? "bg-brand-50 text-brand-700 dark:bg-brand-900/50 dark:text-brand-100" : "active:bg-zinc-100 dark:active:bg-zinc-800"
                  }`
                }
              >
                <Icon name={link.icon} />
                <span className="flex-1">{link.label}</span>
                <Badge count={"badge" in link ? link.badge : undefined} />
              </NavLink>
            ))}
            <div className="mt-2 border-t border-zinc-200 px-1 pt-3 dark:border-zinc-800">
              <ThemePicker large />
            </div>
          </div>
        </div>
      )}
      <nav
        className="fixed inset-x-0 bottom-0 z-50 flex h-[calc(3.5rem+env(safe-area-inset-bottom))] kb:hidden border-t border-zinc-200 bg-white/90 pb-[env(safe-area-inset-bottom)] backdrop-blur-lg md:hidden dark:border-zinc-800 dark:bg-zinc-950/90"
        aria-label="Main"
      >
        {tabs.map((link) => (
          <NavLink key={link.to} to={link.to} className={({ isActive }) => tab(isActive && !more)}>
            <span className="relative">
              <Icon name={link.icon} className="h-[1.35rem] w-[1.35rem]" />
              {"badge" in link && !!link.badge && <span className="bg-brand-600 absolute -top-1 -right-1.5 h-2 w-2 rounded-full ring-2 ring-white dark:ring-zinc-950" />}
            </span>
            {link.short}
          </NavLink>
        ))}
        <button className={tab(more || inOverflow)} onClick={() => setMore(!more)} aria-expanded={more}>
          <span className="relative">
            <Icon name="more" className="h-[1.35rem] w-[1.35rem]" />
            {active > 0 && <span className="bg-brand-600 absolute -top-1 -right-1.5 h-2 w-2 rounded-full ring-2 ring-white dark:ring-zinc-950" />}
          </span>
          More
        </button>
      </nav>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/read/:bookId/*" element={<Reader />} />
      <Route element={<Shell />}>
        {/* The app opens on Ask AI. */}
        <Route path="/" element={<Navigate to="/ask" replace />} />
        <Route path="/library" element={<Library />} />
        <Route path="/ask/:chatId?" element={<Ask />} />
        <Route path="/search" element={<Search />} />
        <Route path="/updates" element={<Updates />} />
        <Route path="/downloads" element={<Downloads />} />
        <Route path="/catalog" element={<Catalog />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="*" element={<Navigate to="/ask" replace />} />
      </Route>
    </Routes>
  );
}
