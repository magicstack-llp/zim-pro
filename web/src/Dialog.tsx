import { useEffect, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useScrollLock } from "./lib/viewport";

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
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return createPortal(
    <div className="fixed inset-x-0 z-[60] flex items-end justify-center sm:items-center sm:p-6" style={{ top: "var(--vv-top, 0px)", height: "var(--vvh, 100dvh)" }}>
      <button className="absolute inset-0 hidden cursor-default bg-black/45 sm:block" aria-label="Close" tabIndex={-1} onClick={onClose} />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="relative flex h-full w-full flex-col bg-white shadow-2xl sm:h-auto sm:max-h-full sm:max-w-lg sm:rounded-3xl dark:bg-zinc-900"
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
