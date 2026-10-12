import { useSyncExternalStore } from "react";

export type Theme = "system" | "light" | "dark";

const KEY = "theme";
const dark = window.matchMedia("(prefers-color-scheme: dark)");
const listeners = new Set<() => void>();

export function getTheme(): Theme {
  try {
    const saved = localStorage.getItem(KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    /* storage is blocked: follow the device */
  }
  return "system";
}

/** Put the chosen theme on the page. index.html does the same before first paint. */
function apply() {
  const theme = getTheme();
  const isDark = theme === "dark" || (theme === "system" && dark.matches);
  const root = document.documentElement;
  root.classList.toggle("dark", isDark);
  root.style.colorScheme = isDark ? "dark" : "light";
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", isDark ? "#09090b" : "#fafafa");
}

export function setTheme(theme: Theme) {
  try {
    if (theme === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, theme);
  } catch {
    /* not remembered, but still applied until the page is closed */
  }
  apply();
  listeners.forEach((notify) => notify());
}

export function installTheme() {
  apply();
  dark.addEventListener("change", apply);
  // Another tab changed the setting.
  window.addEventListener("storage", (event) => {
    if (event.key !== KEY) return;
    apply();
    listeners.forEach((notify) => notify());
  });
}

export function useTheme(): Theme {
  return useSyncExternalStore((notify) => {
    listeners.add(notify);
    return () => listeners.delete(notify);
  }, getTheme);
}
