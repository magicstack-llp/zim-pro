import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useScrollLock } from "./lib/viewport";

const open: object[] = [];

/**
 * A modal for a focused task such as filling in a form.
 *
 * On a desktop it is a centred card. On a phone it fills the visible screen, with the title
 * bar pinned to the top and the actions pinned to the bottom, so the buttons stay in reach
 * just above the keyboard instead of ending up underneath it.
 */
export function Dialog({
  title,
  onClose,
  onBack,
  footer,
  children,
}: {
  title: string;
  onClose: () => void;
  /** Shows a back arrow in place of the title's left edge, for multi-step dialogs. */
  onBack?: () => void;
  footer?: ReactNode;
  children: ReactNode;
}) {
  useScrollLock(true);
  const panel = useRef<HTMLDivElement>(null);
  // Escape closes only the dialog on top, when one was opened from another.
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const mine = {};
    const previous = document.activeElement as HTMLElement | null;
    open.push(mine);

    const focusables = () =>
      Array.from(
        panel.current?.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      ).filter((el) => el.offsetParent !== null || el === document.activeElement);

    if (panel.current && !panel.current.contains(document.activeElement)) {
      const initial = panel.current.querySelector<HTMLElement>("input:not([disabled]), select:not([disabled]), textarea:not([disabled])") ?? focusables()[0] ?? panel.current;
      initial.focus();
    }

    const onKey = (event: KeyboardEvent) => {
      if (open[open.length - 1] !== mine) return;
      if (event.key === "Escape") {
        close.current();
        return;
      }
      if (event.key === "Tab") {
        const items = focusables();
        if (!items.length) {
          event.preventDefault();
          panel.current?.focus();
          return;
        }
        const first = items[0];
        const last = items[items.length - 1];
        if (event.shiftKey && (document.activeElement === first || !panel.current?.contains(document.activeElement))) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && (document.activeElement === last || !panel.current?.contains(document.activeElement))) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      open.splice(open.indexOf(mine), 1);
      previous?.focus?.();
    };
  }, []);

  return createPortal(
    <div className="fixed inset-x-0 z-[60] flex items-end justify-center sm:items-center sm:p-6" style={{ top: "var(--vv-top, 0px)", height: "var(--vvh, 100dvh)" }}>
      <button className="absolute inset-0 hidden cursor-default bg-black/45 sm:block" aria-label="Close" tabIndex={-1} onClick={onClose} />
      <div
        ref={panel}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="relative flex h-full w-full flex-col bg-white shadow-2xl outline-none sm:h-auto sm:max-h-full sm:max-w-lg sm:rounded-3xl dark:bg-zinc-900"
      >
        <header className="flex shrink-0 items-center gap-1 border-b border-zinc-200 py-2 pr-2 pl-2 sm:pl-3 dark:border-zinc-800">
          {onBack && (
            <button type="button" className="flex h-10 w-10 items-center justify-center rounded-full hover:bg-zinc-100 dark:hover:bg-zinc-800" onClick={onBack} aria-label="Back">
              <svg viewBox="0 0 20 20" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                <path d="M12 4.5 6.5 10l5.5 5.5" />
              </svg>
            </button>
          )}
          <h2 className={`min-w-0 flex-1 truncate text-base font-semibold ${onBack ? "" : "pl-2"}`}>{title}</h2>
          <button type="button" className="flex h-10 w-10 items-center justify-center rounded-full hover:bg-zinc-100 dark:hover:bg-zinc-800" onClick={onClose} aria-label="Close">
            <svg viewBox="0 0 20 20" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden>
              <path d="m5.5 5.5 9 9M14.5 5.5l-9 9" />
            </svg>
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-4 sm:px-5">{children}</div>
        {footer && (
          <footer className="shrink-0 border-t border-zinc-200 px-4 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] kb:pb-3 sm:px-5 dark:border-zinc-800">
            {footer}
          </footer>
        )}
      </div>
    </div>,
    document.body,
  );
}
