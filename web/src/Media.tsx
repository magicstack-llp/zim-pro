import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useScrollLock } from "./lib/viewport";

/** A picture, video or recording found on a page. */
export interface Media {
  kind: "image" | "video" | "audio";
  url: string;
  poster?: string;
  caption?: string;
  /** The page it is from, when shown among media from several pages. */
  from?: string;
}

const PLAY = "M7 4.5v11l9-5.5z";
const NOTE = "M8 15V5l8-1.5V13M8 15a2 2 0 1 1-4 0 2 2 0 0 1 4 0Zm8-2a2 2 0 1 1-4 0 2 2 0 0 1 4 0Z";

function Thumb({ item }: { item: Media }) {
  const picture = item.kind === "image" ? item.url : item.poster;
  return (
    <>
      {picture ? (
        <img src={picture} alt="" loading="lazy" className="h-full w-full object-cover" />
      ) : (
        <span className="block h-full w-full bg-zinc-800" />
      )}
      {item.kind !== "image" && (
        <span className="absolute inset-0 flex items-center justify-center bg-black/30">
          <svg viewBox="0 0 20 20" className="h-6 w-6 text-white drop-shadow" fill={item.kind === "video" ? "currentColor" : "none"} stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" aria-hidden>
            <path d={item.kind === "video" ? PLAY : NOTE} />
          </svg>
        </span>
      )}
    </>
  );
}

const label = (item: Media) => item.caption || { image: "Picture", video: "Video", audio: "Recording" }[item.kind];

/** A row of thumbnails; each opens the full picture or plays the video. */
export function MediaStrip({ media, title, show = 4 }: { media: Media[]; title: string; show?: number }) {
  const [open, setOpen] = useState<number | null>(null);
  if (!media.length) return null;
  const hidden = media.length - show;
  return (
    <>
      <ul className="mt-2 flex gap-1.5">
        {media.slice(0, show).map((item, index) => (
          <li key={item.url} className="min-w-0 flex-1">
            <button
              type="button"
              className="hover:ring-brand-500 relative block aspect-square w-full overflow-hidden rounded-lg bg-zinc-100 ring-1 ring-zinc-200 transition hover:ring-2 dark:bg-zinc-800 dark:ring-zinc-700"
              title={label(item)}
              aria-label={label(item)}
              onClick={() => setOpen(index)}
            >
              <Thumb item={item} />
              {index === show - 1 && hidden > 0 && (
                <span className="absolute inset-0 flex items-center justify-center bg-black/55 text-sm font-semibold text-white">+{hidden}</span>
              )}
            </button>
          </li>
        ))}
        {/* Keep thumbnails the same size when a source has fewer than a full row. */}
        {Array.from({ length: Math.max(0, show - media.length) }, (_, index) => (
          <li key={index} className="flex-1" aria-hidden />
        ))}
      </ul>
      {open !== null && <Lightbox media={media} start={open} title={title} onClose={() => setOpen(null)} />}
    </>
  );
}

/** The media of several pages in one scrolling row, e.g. under an answer. */
export function MediaGallery({ groups }: { groups: { title: string; media?: Media[] }[] }) {
  const [open, setOpen] = useState<number | null>(null);
  const seen = new Set<string>();
  const media: Media[] = [];
  // One from each page in turn, so the row is not all from the first source.
  for (let round = 0; media.length < 24 && groups.some((g) => (g.media?.length ?? 0) > round); round++) {
    for (const group of groups) {
      const item = group.media?.[round];
      if (!item || seen.has(item.url)) continue;
      seen.add(item.url);
      media.push({ ...item, from: group.title });
    }
  }
  if (!media.length) return null;
  return (
    <>
      <ul className="-mx-1 mt-4 flex snap-x gap-2 overflow-x-auto px-1 pt-1 pb-2" aria-label="Pictures and videos from the sources">
        {media.map((item, index) => (
          <li key={item.url} className="shrink-0 snap-start">
            <button
              type="button"
              className="hover:ring-brand-500 relative block h-28 overflow-hidden rounded-xl bg-white ring-1 ring-zinc-200 transition hover:ring-2 sm:h-32 dark:bg-zinc-800 dark:ring-zinc-700"
              title={[label(item), item.from].filter(Boolean).join(" — ")}
              aria-label={label(item)}
              onClick={() => setOpen(index)}
            >
              {item.kind === "image" || item.poster ? (
                <img src={item.kind === "image" ? item.url : item.poster} alt="" loading="lazy" className="h-full w-auto max-w-64 min-w-20 object-cover" />
              ) : (
                <span className="block h-full w-40 bg-zinc-800" />
              )}
              {item.kind !== "image" && (
                <span className="absolute inset-0 flex items-center justify-center bg-black/30">
                  <svg viewBox="0 0 20 20" className="h-8 w-8 text-white drop-shadow" fill={item.kind === "video" ? "currentColor" : "none"} stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" aria-hidden>
                    <path d={item.kind === "video" ? PLAY : NOTE} />
                  </svg>
                </span>
              )}
            </button>
          </li>
        ))}
      </ul>
      {open !== null && <Lightbox media={media} start={open} title="" onClose={() => setOpen(null)} />}
    </>
  );
}

function Lightbox({ media, start, title, onClose }: { media: Media[]; start: number; title: string; onClose: () => void }) {
  const [index, setIndex] = useState(start);
  const item = media[index];
  const many = media.length > 1;
  useScrollLock(true);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (event.key === "ArrowRight") setIndex((i) => (i + 1) % media.length);
      if (event.key === "ArrowLeft") setIndex((i) => (i - 1 + media.length) % media.length);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [media.length, onClose]);

  const arrow = "absolute top-1/2 flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-full bg-white/10 text-2xl text-white hover:bg-white/25";
  return createPortal(
    <div role="dialog" aria-modal="true" aria-label={`Media from ${item.from ?? title}`} className="fixed inset-0 z-[70] flex flex-col bg-black/90 text-white" onClick={onClose}>
      <header className="flex shrink-0 items-center gap-3 py-2 pr-2 pl-4" onClick={(event) => event.stopPropagation()}>
        <p className="min-w-0 flex-1 truncate text-sm font-medium">{item.from ?? title}</p>
        {many && (
          <span className="text-xs text-white/70">
            {index + 1} / {media.length}
          </span>
        )}
        <a className="rounded-full px-3 py-2 text-sm hover:bg-white/15" href={item.url} target="_blank" rel="noreferrer">
          Open
        </a>
        <button className="flex h-10 w-10 items-center justify-center rounded-full hover:bg-white/15" onClick={onClose} aria-label="Close">
          <svg viewBox="0 0 20 20" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden>
            <path d="m5.5 5.5 9 9M14.5 5.5l-9 9" />
          </svg>
        </button>
      </header>
      <div className="relative flex min-h-0 flex-1 items-center justify-center px-3">
        {item.kind === "image" && (
          <img key={item.url} src={item.url} alt={item.caption ?? ""} className="max-h-full max-w-full rounded bg-white object-contain" onClick={(event) => event.stopPropagation()} />
        )}
        {item.kind === "video" && (
          <video key={item.url} src={item.url} poster={item.poster} controls autoPlay playsInline className="max-h-full max-w-full rounded" onClick={(event) => event.stopPropagation()} />
        )}
        {item.kind === "audio" && <audio key={item.url} src={item.url} controls autoPlay className="w-full max-w-md" onClick={(event) => event.stopPropagation()} />}
        {many && (
          <>
            <button
              className={`${arrow} left-2`}
              aria-label="Previous"
              onClick={(event) => {
                event.stopPropagation();
                setIndex((index - 1 + media.length) % media.length);
              }}
            >
              ‹
            </button>
            <button
              className={`${arrow} right-2`}
              aria-label="Next"
              onClick={(event) => {
                event.stopPropagation();
                setIndex((index + 1) % media.length);
              }}
            >
              ›
            </button>
          </>
        )}
      </div>
      <p className="mx-auto min-h-12 max-w-3xl shrink-0 px-4 py-3 text-center text-sm text-white/85" onClick={(event) => event.stopPropagation()}>
        {item.caption}
      </p>
    </div>,
    document.body,
  );
}
