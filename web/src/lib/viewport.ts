import { useEffect, useState } from "react";

/**
 * Tracks the part of the screen that is actually visible on a phone.
 *
 * A phone's on-screen keyboard (and its browser's own bars) cover part of the page. Android
 * shrinks the page when the keyboard opens; iOS leaves the page alone and only shrinks the
 * "visual viewport". Either way, this publishes the visible height as `--vvh` and marks the
 * document `keyboard-open`, so layouts can fit what is left and drop anything non-essential
 * (the tab bar, decorative headers) while someone is typing.
 */
export function installViewport(): void {
  const root = document.documentElement;
  const viewport = window.visualViewport;
  let tallest = 0;
  let width = 0;

  const editing = () => {
    const element = document.activeElement as HTMLElement | null;
    if (!element) return false;
    if (element.isContentEditable || element.tagName === "TEXTAREA") return true;
    return element.tagName === "INPUT" && !/^(checkbox|radio|button|submit|range|file|color)$/.test((element as HTMLInputElement).type);
  };
  const update = () => {
    const height = viewport?.height ?? window.innerHeight;
    if (window.innerWidth !== width) {
      // Rotated or resized: what counts as "full height" starts over.
      width = window.innerWidth;
      tallest = 0;
    }
    tallest = Math.max(tallest, height);
    root.style.setProperty("--vvh", `${Math.round(height)}px`);
    root.style.setProperty("--vv-top", `${Math.round(viewport?.offsetTop ?? 0)}px`);
    root.classList.toggle("keyboard-open", editing() && tallest - height > 120);
    // iOS pans the page to reveal a focused field; full-screen views are already sized to
    // fit above the keyboard, so undo the pan rather than leave the header off-screen.
    if (root.classList.contains("app-screen") && window.scrollY !== 0) window.scrollTo(0, 0);
  };

  viewport?.addEventListener("resize", update);
  viewport?.addEventListener("scroll", update);
  window.addEventListener("resize", update);
  document.addEventListener("focusin", update);
  // The keyboard closes a moment after focus leaves; look again once it has.
  document.addEventListener("focusout", () => setTimeout(update, 60));
  update();
}

/** Mark the document while a full-screen, app-style view is showing. */
export function useAppScreen(): void {
  useEffect(() => {
    document.documentElement.classList.add("app-screen");
    return () => document.documentElement.classList.remove("app-screen");
  }, []);
}

function useMedia(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const media = window.matchMedia(query);
    const onChange = () => setMatches(media.matches);
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}

/** Narrow screens get sheets and full-screen pickers instead of popovers. */
export const usePhone = () => useMedia("(max-width: 639px)");
/** Room for a permanent sources column beside the answer. */
export const useWide = () => useMedia("(min-width: 1280px)");
/** Touch-first devices: no autofocus (it throws the keyboard up) and Enter means a new line. */
export const useTouch = () => useMedia("(pointer: coarse)");

/** Stop the page behind a dialog from scrolling while it is open. */
export function useScrollLock(locked: boolean): void {
  useEffect(() => {
    if (!locked) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [locked]);
}
