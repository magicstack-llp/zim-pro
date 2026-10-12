import { NavLink, Outlet, Route, Routes } from "react-router-dom";
import type { UpdateItem } from "./lib/api";
import { useData, useLive } from "./lib/live";
import Ask from "./pages/Ask";
import Catalog from "./pages/Catalog";
import Downloads from "./pages/Downloads";
import Library from "./pages/Library";
import Reader from "./pages/Reader";
import Search from "./pages/Search";
import Settings from "./pages/Settings";
import Updates from "./pages/Updates";

function Shell() {
  const { downloads } = useLive();
  const updates = useData<{ update_count: number; items: UpdateItem[] }>("/api/updates");
  const active = downloads.filter((d) => ["queued", "downloading", "verifying"].includes(d.state)).length;
  const links = [
    { to: "/ask", label: "Ask AI" },
    { to: "/", label: "Library" },
    { to: "/search", label: "Search" },
    { to: "/updates", label: "Updates", badge: updates.data?.update_count },
    { to: "/downloads", label: "Downloads", badge: active },
    { to: "/catalog", label: "Catalog" },
    { to: "/settings", label: "Settings" },
  ];
  return (
    <div className="flex min-h-full flex-col md:flex-row">
      <nav className="sticky top-0 z-20 flex shrink-0 items-center gap-1 overflow-x-auto border-b border-zinc-200 bg-white/90 px-3 py-2 backdrop-blur md:h-screen md:w-52 md:flex-col md:items-stretch md:border-r md:border-b-0 md:px-3 md:py-5 dark:border-zinc-800 dark:bg-zinc-900/90">
        <div className="mr-3 flex items-center gap-2 px-1 md:mr-0 md:mb-5">
          <div className="bg-brand-700 flex h-8 w-8 items-center justify-center rounded-lg font-bold text-white">K</div>
          <span className="hidden font-semibold md:block">Kiwix Library</span>
        </div>
        {links.map((link) => (
          <NavLink
            key={link.to}
            to={link.to}
            end={link.to === "/"}
            className={({ isActive }) =>
              `flex items-center justify-between gap-2 rounded-lg px-3 py-1.5 text-sm font-medium whitespace-nowrap ${
                isActive
                  ? "bg-brand-50 text-brand-700 dark:bg-brand-900/50 dark:text-brand-100"
                  : "text-zinc-600 hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-800"
              }`
            }
          >
            {link.label}
            {!!link.badge && (
              <span className="bg-brand-600 rounded-full px-1.5 text-xs leading-5 font-semibold text-white">
                {link.badge}
              </span>
            )}
          </NavLink>
        ))}
      </nav>
      <main className="min-w-0 flex-1 px-4 py-6 md:px-8">
        <div className="mx-auto max-w-6xl">
          <Outlet />
        </div>
      </main>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/read/:bookId/*" element={<Reader />} />
      <Route element={<Shell />}>
        <Route path="/" element={<Library />} />
        <Route path="/ask/:chatId?" element={<Ask />} />
        <Route path="/search" element={<Search />} />
        <Route path="/updates" element={<Updates />} />
        <Route path="/downloads" element={<Downloads />} />
        <Route path="/catalog" element={<Catalog />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="*" element={<Library />} />
      </Route>
    </Routes>
  );
}
