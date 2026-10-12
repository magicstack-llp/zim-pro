import { setTheme, useTheme, type Theme } from "./lib/theme";

const OPTIONS: { value: Theme; label: string; icon: string }[] = [
  { value: "light", label: "Light", icon: "M10 13a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M4.7 15.3l1.4-1.4M13.9 6.1l1.4-1.4" },
  { value: "dark", label: "Dark", icon: "M16.5 11.5A7 7 0 0 1 8.5 3.5a7 7 0 1 0 8 8z" },
  { value: "system", label: "Auto", icon: "M3.5 4.5h13v8.5h-13zM7.5 16.5h5M10 13v3.5" },
];

/** Light, dark, or whatever the device is set to. */
export function ThemePicker({ large = false }: { large?: boolean }) {
  const theme = useTheme();
  return (
    <div role="radiogroup" aria-label="Theme" className="flex gap-0.5 rounded-xl bg-zinc-100 p-0.5 dark:bg-zinc-800">
      {OPTIONS.map((option) => {
        const chosen = theme === option.value;
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={chosen}
            title={option.value === "system" ? "Follow this device" : `${option.label} theme`}
            onClick={() => setTheme(option.value)}
            className={`flex min-w-0 flex-1 items-center justify-center gap-1.5 rounded-[10px] font-medium transition ${large ? "py-2.5 text-sm" : "py-1.5 text-xs"} ${
              chosen ? "bg-white text-zinc-900 shadow-sm dark:bg-zinc-600 dark:text-white" : "text-zinc-500 hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-100"
            }`}
          >
            <svg viewBox="0 0 20 20" className={large ? "h-4.5 w-4.5" : "h-3.5 w-3.5"} fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d={option.icon} />
            </svg>
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
