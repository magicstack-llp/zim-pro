import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { usePhone, useScrollLock, useTouch } from "./lib/viewport";

export interface Option {
  value: string;
  label: string;
  hint?: string;
  /** A second line under the label. */
  description?: string;
  /** Text for a round badge before the label, e.g. a provider's initial. */
  badge?: string;
  /** Rendered as a section heading above this option. */
  group?: string;
}

const LIMIT = 80;

function Badge({ text }: { text: string }) {
  return (
    <span
      aria-hidden
      className="bg-brand-100 text-brand-700 dark:bg-brand-900 dark:text-brand-100 flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-xs font-semibold"
    >
      {text}
    </span>
  );
}

/** A dropdown with a search box, for lists too long for a plain select. */
export function Combobox({
  value,
  onChange,
  options,
  placeholder,
  searchPlaceholder = "Search…",
  className = "",
  align = "left",
  up = false,
  active = false,
  allowCustom = false,
  wide = false,
  children,
}: {
  value: string;
  onChange: (value: string) => void;
  options: Option[];
  /** Shown on the button when nothing in `options` is selected. */
  placeholder: string;
  searchPlaceholder?: string;
  className?: string;
  align?: "left" | "right";
  /** Open above the button, for a control near the bottom of the page. */
  up?: boolean;
  /** Style the button as selected even when showing the placeholder. */
  active?: boolean;
  /** Offer whatever was typed as a value when it is not in the list. */
  allowCustom?: boolean;
  /** Stretch the button and the list to the width of the container. */
  wide?: boolean;
  children?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const [cursor, setCursor] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const sheet = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const phone = usePhone();
  const touch = useTouch();
  useScrollLock(open && phone);
  const selected = options.find((option) => option.value === value);

  const matches = useMemo(() => {
    const words = filter.toLowerCase().split(/\s+/).filter(Boolean);
    return options.filter((option) => {
      const haystack = `${option.label} ${option.hint ?? ""} ${option.value}`.toLowerCase();
      return words.every((word) => haystack.includes(word));
    });
  }, [options, filter]);
  const typed = filter.trim();
  const custom: Option[] =
    allowCustom && typed && !options.some((option) => option.value === typed)
      ? [{ value: typed, label: `Use “${typed}”`, hint: "custom" }]
      : [];
  const shown = [...matches.slice(0, LIMIT), ...custom];

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!root.current?.contains(target) && !sheet.current?.contains(target)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  useEffect(() => setCursor(0), [filter, open]);
  useEffect(() => {
    list.current?.querySelector('[data-active="true"]')?.scrollIntoView({ block: "nearest" });
  }, [cursor]);

  const pick = (option: Option) => {
    onChange(option.value);
    setOpen(false);
    setFilter("");
  };

  return (
    <div ref={root} className={`relative max-w-full min-w-0 ${wide ? "block w-full" : "inline-flex"}`}>
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className={`min-w-0 ${className} ${selected || active ? "is-selected" : ""}`}
      >
        {children}
        {selected?.badge && <Badge text={selected.badge} />}
        <span className={`truncate ${wide ? "flex-1 text-left" : ""} ${selected || !wide ? "" : "muted"}`}>
          {selected?.label ?? (allowCustom && value ? value : placeholder)}
        </span>
        <svg viewBox="0 0 12 12" className="h-3 w-3 shrink-0 opacity-60" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
          <path d="m2.5 4.5 3.5 3.5 3.5-3.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {open &&
        (phone ? (
          // On a phone a picker takes the whole visible screen: big rows, and a list that
          // still fits when the keyboard comes up for the search box.
          createPortal(
            <div
              ref={sheet}
              role="dialog"
              aria-label={placeholder}
              className="fixed inset-x-0 z-[70] flex flex-col bg-white text-left dark:bg-zinc-900"
              style={{ top: "var(--vv-top, 0px)", height: "var(--vvh, 100dvh)" }}
            >
              <div className="flex items-center justify-between gap-3 px-4 pt-3 pb-1">
                <span className="truncate text-base font-semibold">{placeholder}</span>
                <button type="button" className="btn" onClick={() => setOpen(false)}>
                  Close
                </button>
              </div>
              <div className="border-b border-zinc-200 p-2 dark:border-zinc-800">
            <input
              autoFocus={!touch}
              type="search"
              enterKeyHint="search"
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
              className="input"
              placeholder={searchPlaceholder}
              aria-label={searchPlaceholder}
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown") (e.preventDefault(), setCursor(Math.min(cursor + 1, shown.length - 1)));
                else if (e.key === "ArrowUp") (e.preventDefault(), setCursor(Math.max(cursor - 1, 0)));
                else if (e.key === "Enter") (e.preventDefault(), shown[cursor] && pick(shown[cursor]));
                else if (e.key === "Escape") setOpen(false);
              }}
            />
          </div>
          <ul ref={list} role="listbox" className={phone ? "min-h-0 flex-1 overflow-y-auto overscroll-contain py-1" : "max-h-72 overflow-y-auto py-1"}>
            {shown.map((option, index) => (
              <li key={option.value} role="presentation">
                {option.group && (index === 0 || shown[index - 1].group !== option.group) && (
                  <div className="muted px-3 pt-2 pb-1 text-xs font-semibold tracking-wide uppercase">{option.group}</div>
                )}
                <button
                  type="button"
                  role="option"
                  aria-selected={option.value === value}
                  data-active={index === cursor}
                  onMouseEnter={() => setCursor(index)}
                  onClick={() => pick(option)}
                  className={`flex w-full items-center gap-2.5 px-4 py-3 text-left text-base sm:px-3 sm:py-1.5 sm:text-sm ${
                    index === cursor ? "bg-zinc-100 dark:bg-zinc-800" : ""
                  } ${option.value === value ? "text-brand-700 dark:text-brand-400 font-medium" : ""}`}
                >
                  {option.badge && <Badge text={option.badge} />}
                  <span className="min-w-0 flex-1">
                    <span className="block truncate">{option.label}</span>
                    {option.description && (
                      <span className="muted block truncate text-xs font-normal">{option.description}</span>
                    )}
                  </span>
                  {option.hint && <span className="muted shrink-0 text-xs font-normal">{option.hint}</span>}
                  {option.value === value && (
                    <svg viewBox="0 0 16 16" className="h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
                      <path d="m3.5 8.5 3 3 6-7" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  )}
                </button>
              </li>
            ))}
            {shown.length === 0 && (
              <li className="muted px-3 py-3 text-sm">{typed ? `Nothing matches “${typed}”` : "Nothing to choose from yet"}</li>
            )}
            {matches.length > LIMIT && (
              <li className="muted px-3 py-2 text-xs">
                Showing {LIMIT} of {matches.length}. Type to narrow the list.
              </li>
            )}
          </ul>
            </div>,
            document.body,
          )
        ) : (
          <div
            className={`card absolute z-40 overflow-hidden text-left shadow-xl ${wide ? "w-full" : "w-80"} ${
              up ? "bottom-full mb-1.5" : "top-full mt-1.5"
            } ${align === "right" ? "right-0" : "left-0"}`}
          >
            <div className="border-b border-zinc-200 p-2 dark:border-zinc-800">
            <input
              autoFocus={!touch}
              type="search"
              enterKeyHint="search"
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
              className="input"
              placeholder={searchPlaceholder}
              aria-label={searchPlaceholder}
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown") (e.preventDefault(), setCursor(Math.min(cursor + 1, shown.length - 1)));
                else if (e.key === "ArrowUp") (e.preventDefault(), setCursor(Math.max(cursor - 1, 0)));
                else if (e.key === "Enter") (e.preventDefault(), shown[cursor] && pick(shown[cursor]));
                else if (e.key === "Escape") setOpen(false);
              }}
            />
          </div>
          <ul ref={list} role="listbox" className={phone ? "min-h-0 flex-1 overflow-y-auto overscroll-contain py-1" : "max-h-72 overflow-y-auto py-1"}>
            {shown.map((option, index) => (
              <li key={option.value} role="presentation">
                {option.group && (index === 0 || shown[index - 1].group !== option.group) && (
                  <div className="muted px-3 pt-2 pb-1 text-xs font-semibold tracking-wide uppercase">{option.group}</div>
                )}
                <button
                  type="button"
                  role="option"
                  aria-selected={option.value === value}
                  data-active={index === cursor}
                  onMouseEnter={() => setCursor(index)}
                  onClick={() => pick(option)}
                  className={`flex w-full items-center gap-2.5 px-4 py-3 text-left text-base sm:px-3 sm:py-1.5 sm:text-sm ${
                    index === cursor ? "bg-zinc-100 dark:bg-zinc-800" : ""
                  } ${option.value === value ? "text-brand-700 dark:text-brand-400 font-medium" : ""}`}
                >
                  {option.badge && <Badge text={option.badge} />}
                  <span className="min-w-0 flex-1">
                    <span className="block truncate">{option.label}</span>
                    {option.description && (
                      <span className="muted block truncate text-xs font-normal">{option.description}</span>
                    )}
                  </span>
                  {option.hint && <span className="muted shrink-0 text-xs font-normal">{option.hint}</span>}
                  {option.value === value && (
                    <svg viewBox="0 0 16 16" className="h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
                      <path d="m3.5 8.5 3 3 6-7" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  )}
                </button>
              </li>
            ))}
            {shown.length === 0 && (
              <li className="muted px-3 py-3 text-sm">{typed ? `Nothing matches “${typed}”` : "Nothing to choose from yet"}</li>
            )}
            {matches.length > LIMIT && (
              <li className="muted px-3 py-2 text-xs">
                Showing {LIMIT} of {matches.length}. Type to narrow the list.
              </li>
            )}
          </ul>
          </div>
        ))}
    </div>
  );
}
