import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from "react";
import Markdown from "react-markdown";
import { createPortal } from "react-dom";
import { Link, useNavigate, useParams } from "react-router-dom";
import remarkGfm from "remark-gfm";
import { Combobox, type Option } from "../Combobox";
import { modelKey, modelOptions, shortModel, splitModelKey, useModels } from "../lib/ai";
import { api, authHeaders, readUrl, type Book, type Status } from "../lib/api";
import { categoryCounts, categoryLabel } from "../lib/format";
import { useData } from "../lib/live";
import { speak, stopSpeaking } from "../lib/speech";
import { tidyMarkdown } from "../lib/tidy";
import { useAppScreen, useScrollLock, useTouch, useWide } from "../lib/viewport";
import { MediaGallery, MediaStrip, type Media } from "../Media";
import { bookOptions } from "../Scope";
import { BookIcon, ErrorNote } from "../ui";

interface Source {
  n: number;
  kind?: "library" | "web";
  title: string;
  excerpt: string;
  book: string;
  book_title: string;
  path: string;
  url?: string;
  site?: string;
  /** Pictures, videos and recordings on the source's page. */
  media?: Media[];
}
/** One thing the app did to answer: a search it ran, shown like a tool call. */
interface Step {
  tool: "plan" | "library" | "web";
  title: string;
  state: "running" | "done" | "failed" | "stopped";
  queries?: string[];
  /** Extra terms a good article would contain, beyond the queries. */
  keywords?: string[];
  /** Titles of the books chosen to search in depth. */
  books?: string[];
  results?: { title: string; where: string }[];
  detail?: string;
  seconds?: number;
}
interface Message {
  role: "user" | "assistant";
  content: string;
  sources?: Source[];
  queries?: string[];
  status?: string;
  notice?: string;
  error?: string;
  model?: string;
  pending?: boolean;
  steps?: Step[];
  /** The model's thinking, kept apart from the answer. */
  reasoning?: string;
  thought_seconds?: number | null;
  /** When thinking started in this tab, for the live timer. */
  thinkingSince?: number;
}
interface Chat {
  id: string;
  title: string;
  updated: number;
}

/** The reading column, with a sources column beside it when the screen is wide enough. */
const COLUMN = "mx-auto w-full max-w-3xl px-4 xl:max-w-[72rem] xl:px-8";
const ROW = "xl:grid xl:grid-cols-[minmax(0,1fr)_19rem] xl:gap-10";
const ICON_BUTTON =
  "flex h-10 w-10 sm:h-8 sm:w-8 items-center justify-center rounded-lg text-zinc-500 transition hover:bg-zinc-200/70 hover:text-zinc-900 disabled:opacity-40 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100";
const PILL =
  "flex h-9 sm:h-8 items-center gap-1.5 rounded-full px-2.5 text-[13px] text-zinc-600 transition hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-800";
const PANEL_KEY = "kiwix-ask-panel";
const MODEL_KEY = "kiwix-ask-model";

const SUGGESTIONS: Record<string, [string, string][]> = {
  wikipedia: [
    ["Explain", "How do vaccines train the immune system?"],
    ["Summarise", "What caused the First World War?"],
  ],
  devdocs: [
    ["Code", "How do I reverse a list in Python?"],
    ["Code", "What does git rebase do, and when should I use it?"],
  ],
  stack_exchange: [["How-to", "How do I free up disk space on Ubuntu?"]],
  iFixit: [["Repair", "How do I replace a cracked phone screen?"]],
  other: [["Survival", "How can I make water safe to drink in an emergency?"]],
};

function Icon({ d, className = "h-4 w-4" }: { d: string; className?: string }) {
  return (
    <svg viewBox="0 0 20 20" className={className} fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d={d} />
    </svg>
  );
}
const ICONS = {
  plus: "M10 4v12M4 10h12",
  panel: "M3.5 4.5h13v11h-13zM8 4.5v11",
  menu: "M3.5 6h13M3.5 10h13M3.5 14h13",
  send: "M10 16V4.5M5 9.5l5-5 5 5",
  copy: "M7 7V4h9v9h-3M4 7h9v9H4z",
  check: "m4.5 10.5 3.5 3.5 7.5-8",
  redo: "M15.5 8A6 6 0 1 0 16 11.5M15.5 4v4h-4",
  edit: "m12.5 4.5 3 3L8 15l-3.5.5L5 12z",
  trash: "M4.5 6h11M8 6V4h4v2M6 6l.5 10h7L14 6",
  search: "M9 15A6 6 0 1 0 9 3a6 6 0 0 0 0 12zM13.5 13.5 17 17",
  book: "M4 4.5h5a2 2 0 0 1 2 2V16a2 2 0 0 0-2-2H4zM16 4.5h-3a2 2 0 0 0-2 2V16a2 2 0 0 1 2-2h3z",
  globe: "M10 17a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM3 10h14M10 3c2 2 3 4.5 3 7s-1 5-3 7c-2-2-3-4.5-3-7s1-5 3-7z",
  speaker: "M4 8v4h3l4 3.5v-11L7 8zM13.5 7.5a3.5 3.5 0 0 1 0 5M15.5 5.5a6.5 6.5 0 0 1 0 9",
  stop: "M6 6h8v8H6z",
  spark: "M10 3l1.6 4.4L16 9l-4.4 1.6L10 15l-1.6-4.4L4 9l4.4-1.6zM15.5 13.5l.6 1.4 1.4.6-1.4.6-.6 1.4-.6-1.4-1.4-.6 1.4-.6z",
  gear: "M10 12.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5zM10 3v2M10 15v2M3 10h2M15 10h2M5 5l1.5 1.5M13.5 13.5 15 15M15 5l-1.5 1.5M6.5 13.5 5 15",
  external: "M11 4h5v5M16 4l-7 7M14 11v5H4V6h5",
  bulb: "M7.5 14.5h5M8.5 17h3M10 3a5 5 0 0 0-3 9c.6.5 1 1.3 1 2v.5h4V14c0-.7.4-1.5 1-2a5 5 0 0 0-3-9z",
  chevron: "m7.5 4.5 5.5 5.5-5.5 5.5",
};

const isWeb = (source: Source) => source.kind === "web";
const sourceLink = (source: Source) => (isWeb(source) ? (source.url ?? "#") : readUrl(source.book, source.path));

/** Turn "[2]" in the answer into a link the renderer swaps for a citation pill. */
function linkCitations(text: string, sources: Source[] = []): string {
  return text.replace(/\[(\d+)\](?!\()/g, (whole, n) => (sources.some((s) => s.n === +n) ? `[${n}](#cite-${n})` : whole));
}

function SourceIcon({ source, size = 20 }: { source: Source; size?: number }) {
  if (isWeb(source)) {
    return (
      <span className="flex shrink-0 items-center justify-center rounded-md bg-sky-100 text-sky-700 dark:bg-sky-950 dark:text-sky-300" style={{ width: size, height: size }}>
        <Icon d={ICONS.globe} className="h-3.5 w-3.5" />
      </span>
    );
  }
  return <BookIcon src={`/api/books/${encodeURIComponent(source.book)}/illustration`} title={source.book_title} size={size} />;
}

function SourceLink({
  source,
  className,
  children,
  onHover,
  onClick,
}: {
  source: Source;
  className: string;
  children: ReactNode;
  onHover?: (n: number | null) => void;
  /** Return false to stop the link opening, e.g. to show the source in a panel first. */
  onClick?: () => boolean;
}) {
  const hover = {
    onMouseEnter: () => onHover?.(source.n),
    onMouseLeave: () => onHover?.(null),
    onClick: (event: { preventDefault: () => void }) => {
      if (onClick?.() === false) event.preventDefault();
    },
  };
  return isWeb(source) ? (
    <a href={sourceLink(source)} target="_blank" rel="noreferrer" className={className} {...hover}>
      {children}
    </a>
  ) : (
    <Link to={sourceLink(source)} className={className} {...hover}>
      {children}
    </Link>
  );
}

function Spark({ spinning = false }: { spinning?: boolean }) {
  return (
    <span className="from-brand-500 mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-gradient-to-br to-sky-500 text-white shadow-sm" aria-hidden>
      <Icon d={ICONS.spark} className={`h-4 w-4 ${spinning ? "animate-pulse" : ""}`} />
    </span>
  );
}

function SourceCard({ source, hot, onHover }: { source: Source; hot: boolean; onHover?: (n: number | null) => void }) {
  return (
    <div
      className={`rounded-2xl border bg-white p-3 transition hover:-translate-y-px hover:shadow-md dark:bg-zinc-900 ${
        hot ? "border-brand-500 shadow-md" : "border-zinc-200 dark:border-zinc-800"
      }`}
    >
      <SourceLink source={source} onHover={onHover} className="group block">
      <span className="flex items-center gap-2">
        <SourceIcon source={source} />
        <span className="muted min-w-0 flex-1 truncate text-xs">{isWeb(source) ? source.site : source.book_title}</span>
        <span className="flex h-5 min-w-5 items-center justify-center rounded-full bg-zinc-100 px-1 text-[11px] font-semibold text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">
          {source.n}
        </span>
      </span>
      <span className="group-hover:text-brand-700 dark:group-hover:text-brand-400 mt-2 line-clamp-2 text-sm leading-snug font-medium">
        {source.title}
        {isWeb(source) && <Icon d={ICONS.external} className="ml-1 inline h-3 w-3 opacity-50" />}
      </span>
        {source.excerpt && <span className="muted mt-1 line-clamp-3 text-xs leading-relaxed">{source.excerpt}</span>}
      </SourceLink>
      {/* Outside the link: a thumbnail opens the picture, not the article. */}
      <MediaStrip media={source.media ?? []} title={source.title} />
    </div>
  );
}

const sourceCounts = (sources: Source[]) => {
  const library = sources.filter((s) => !isWeb(s)).length;
  const web = sources.length - library;
  return [library && `${library} from your library`, web && `${web} from the web`].filter(Boolean).join(" · ");
};

/** On screens too narrow for a sources column, the sources slide in from the right. */
function SourcesPanel({ sources, hot, onClose }: { sources: Source[]; hot: number | null; onClose: () => void }) {
  const list = useRef<HTMLOListElement>(null);
  useScrollLock(true);
  useEffect(() => {
    list.current?.querySelector('[data-hot="true"]')?.scrollIntoView({ block: "center" });
    const onKey = (event: globalThis.KeyboardEvent) => event.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [hot, onClose]);
  return createPortal(
    <div className="fixed inset-x-0 z-[60]" style={{ top: "var(--vv-top, 0px)", height: "var(--vvh, 100dvh)" }}>
      <button className="fade absolute inset-0 bg-black/45" aria-label="Close sources" onClick={onClose} />
      <aside
        role="dialog"
        aria-label="Sources"
        className="slide-in absolute inset-y-0 right-0 flex w-[88vw] max-w-sm flex-col bg-zinc-50 shadow-2xl dark:bg-zinc-950"
      >
        <header className="flex shrink-0 items-center gap-2 border-b border-zinc-200 py-2 pr-2 pl-4 dark:border-zinc-800">
          <div className="min-w-0 flex-1">
            <h2 className="text-base font-semibold">Sources</h2>
            <p className="muted truncate text-xs">{sourceCounts(sources)}</p>
          </div>
          <button className="flex h-10 w-10 items-center justify-center rounded-full hover:bg-zinc-200/70 dark:hover:bg-zinc-800" onClick={onClose} aria-label="Close">
            <svg viewBox="0 0 20 20" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden>
              <path d="m5.5 5.5 9 9M14.5 5.5l-9 9" />
            </svg>
          </button>
        </header>
        <ol ref={list} className="min-h-0 flex-1 space-y-2 overflow-y-auto overscroll-contain p-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
          {sources.map((source) => (
            <li key={source.n} data-hot={hot === source.n}>
              <SourceCard source={source} hot={hot === source.n} />
            </li>
          ))}
        </ol>
      </aside>
    </div>,
    document.body,
  );
}

const STEP_ICONS: Record<Step["tool"], string> = { plan: ICONS.spark, library: ICONS.book, web: ICONS.globe };

/**
 * What the app did to produce the answer, folded away like the thinking.
 *
 * The app runs the searches itself rather than leaving them to the model, and each one is
 * recorded as a step: what was searched for, where, and what came back.
 */
function Steps({ steps, live }: { steps: Step[]; live: boolean }) {
  const [open, setOpen] = useState(false);
  const running = live ? steps.find((step) => step.state === "running") : undefined;
  const searched = steps.filter((step) => step.tool !== "plan" && step.state !== "running");
  const found = searched.reduce((sum, step) => sum + (step.results?.length ?? 0), 0);
  const bookCount = steps.find((step) => step.tool === "plan")?.books?.length;
  const failed = steps.some((step) => step.state === "failed");
  const places = [searched.some((s) => s.tool === "library") && "your library", searched.some((s) => s.tool === "web") && "the web"].filter(Boolean);
  const label = running
    ? `${running.title}…`
    : places.length
      ? `Searched ${places.join(" and ")} · ${found} ${found === 1 ? "source" : "sources"}${bookCount ? ` · ${bookCount} ${bookCount === 1 ? "book" : "books"} in depth` : ""}`
      : (steps[steps.length - 1]?.title ?? "");
  return (
    <div className="mb-1.5">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className="-ml-2 flex max-w-full items-center gap-1.5 rounded-lg px-2 py-1.5 text-left text-sm text-zinc-500 transition hover:bg-zinc-100 hover:text-zinc-800 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
      >
        <Icon d={ICONS.search} className="h-4 w-4 shrink-0" />
        <span className={`truncate ${running ? "shimmer" : ""}`}>{label}</span>
        {failed && !running && <span className="shrink-0 text-amber-600 dark:text-amber-400">· 1 failed</span>}
        <Icon d={ICONS.chevron} className={`h-3.5 w-3.5 shrink-0 transition-transform ${open ? "rotate-90" : ""}`} />
      </button>
      {open && (
        <ol className="mt-1.5 space-y-3 border-l-2 border-zinc-200 py-1 pl-3.5 dark:border-zinc-700">
          {steps.map((step, index) => (
            <li key={index} className="text-[13px] leading-relaxed">
              <div className="flex items-center gap-1.5 font-medium text-zinc-700 dark:text-zinc-200">
                <Icon d={STEP_ICONS[step.tool]} className="h-3.5 w-3.5 shrink-0 opacity-70" />
                <span className={step.state === "running" ? "shimmer" : ""}>{step.title}</span>
                {step.state === "stopped" && <span className="muted font-normal">· stopped</span>}
                {step.seconds !== undefined && step.seconds >= 0.1 && <span className="muted font-normal">· {step.seconds}s</span>}
              </div>
              {(
                [
                  ["Searches", step.queries, "bg-zinc-100 font-mono text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300"],
                  ["Keywords", step.keywords, "bg-brand-50 font-mono text-brand-900 dark:bg-brand-900/40 dark:text-brand-100"],
                  ["Books", step.books, "bg-sky-50 text-sky-900 dark:bg-sky-950 dark:text-sky-200"],
                ] as const
              ).map(
                ([name, items, tone]) =>
                  !!items?.length && (
                    <div key={name} className="mt-1 flex flex-wrap items-baseline gap-1">
                      {step.tool === "plan" && <span className="muted mr-0.5 w-16 shrink-0 text-[12px]">{name}</span>}
                      {items.map((item) => (
                        <span key={item} className={`rounded-md px-1.5 py-0.5 text-[12px] ${tone}`}>
                          {item}
                        </span>
                      ))}
                    </div>
                  ),
              )}
              {step.detail && (
                <p className={`mt-1 ${step.state === "failed" ? "text-red-600 dark:text-red-400" : "muted"}`}>{step.detail}</p>
              )}
              {!!step.results?.length && (
                <ul className="muted mt-1 space-y-0.5">
                  {step.results.map((result, n) => (
                    <li key={n} className="truncate">
                      {result.title}
                      {result.where && <span className="opacity-70"> — {result.where}</span>}
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

const seconds = (value: number) => (value < 1.5 ? "a second" : value < 90 ? `${Math.round(value)} seconds` : `${Math.round(value / 60)} minutes`);

/**
 * The model's thinking, folded away above the answer.
 *
 * Reasoning models think before they answer. As in other chat apps, that is shown as one
 * quiet line ("Thinking…", then "Thought for 12 seconds") that opens on request: it is useful
 * for seeing how an answer came about, but it is working-out, not the answer.
 */
function Thinking({ message }: { message: Message }) {
  const [open, setOpen] = useState(false);
  const [now, setNow] = useState(Date.now());
  const body = useRef<HTMLDivElement>(null);
  const live = !!message.pending && !message.content;
  useEffect(() => {
    if (!live) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [live]);
  // While it is still thinking, keep the newest line in view.
  useEffect(() => {
    if (live && open && body.current) body.current.scrollTop = body.current.scrollHeight;
  }, [live, open, message.reasoning]);

  const elapsed = live && message.thinkingSince ? (now - message.thinkingSince) / 1000 : message.thought_seconds;
  const label = live
    ? `Thinking${elapsed && elapsed >= 2 ? ` · ${Math.round(elapsed)}s` : "…"}`
    : elapsed
      ? `Thought for ${seconds(elapsed)}`
      : "Thought process";
  return (
    <div className="mb-3">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className="-ml-2 flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-sm text-zinc-500 transition hover:bg-zinc-100 hover:text-zinc-800 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
      >
        <Icon d={ICONS.bulb} className="h-4 w-4 shrink-0" />
        <span className={live ? "shimmer" : ""}>{label}</span>
        <Icon d={ICONS.chevron} className={`h-3.5 w-3.5 transition-transform ${open ? "rotate-90" : ""}`} />
      </button>
      {open && (
        <div
          ref={body}
          className="mt-1.5 max-h-72 overflow-y-auto border-l-2 border-zinc-200 py-1 pr-1 pl-3.5 text-[13px] leading-relaxed whitespace-pre-wrap text-zinc-500 dark:border-zinc-700 dark:text-zinc-400"
        >
          {message.reasoning}
        </div>
      )}
    </div>
  );
}

function Listen({ text, mode }: { text: string; mode: string }) {
  const [state, setState] = useState<"idle" | "loading" | "playing">("idle");
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => () => stopSpeaking(), []);
  const toggle = () => {
    setProblem(null);
    if (state !== "idle") return stopSpeaking();
    speak(text, mode, setState).catch((reason) => setProblem(String(reason.message ?? reason)));
  };
  return (
    <>
      <button className={ICON_BUTTON} onClick={toggle} title={state === "idle" ? "Read aloud" : "Stop reading"} aria-label={state === "idle" ? "Read aloud" : "Stop reading"}>
        {state === "loading" ? (
          <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent" />
        ) : (
          <Icon d={state === "playing" ? ICONS.stop : ICONS.speaker} />
        )}
      </button>
      {problem && <span className="ml-1 text-xs text-red-600 dark:text-red-400">{problem}</span>}
    </>
  );
}

function Answer({
  message,
  ttsMode,
  onRegenerate,
  onShowSources,
}: {
  message: Message;
  ttsMode: string;
  onRegenerate?: () => void;
  /** Set when there is no room for the sources column: opens them in a side panel instead. */
  onShowSources?: (sources: Source[], hot: number | null) => void;
}) {
  const sources = message.sources ?? [];
  // Small models get Markdown wrong in predictable ways; repair it before showing it.
  const content = useMemo(
    () => tidyMarkdown(message.content, { streaming: !!message.pending, hasSources: sources.length > 0 }),
    [message.content, message.pending, sources.length],
  );
  const [copied, setCopied] = useState(false);
  const [hot, setHot] = useState<number | null>(null);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard is unavailable on plain http from another machine */
    }
  };
  return (
    <div className={`rise ${ROW}`} data-answer>
      <div className="flex min-w-0 gap-3.5">
        <span className="hidden sm:block">
          <Spark spinning={message.pending} />
        </span>
        <div className="min-w-0 flex-1">
          {!!message.steps?.length && <Steps steps={message.steps} live={!!message.pending} />}
          {message.reasoning && <Thinking message={message} />}
          {content && (
            <div className={`answer text-base leading-7 ${message.pending ? "streaming" : ""}`}>
              <Markdown
                remarkPlugins={[remarkGfm]}
                components={{
                  a: ({ href = "", children }) => {
                    const cited = href.startsWith("#cite-") ? sources.find((s) => s.n === +href.slice(6)) : undefined;
                    if (cited) {
                      return (
                        <SourceLink
                          source={cited}
                          className={`cite ${hot === cited.n ? "cite-hot" : ""}`}
                          onHover={setHot}
                          onClick={onShowSources ? () => (onShowSources(sources, cited.n), false) : undefined}
                        >
                          {children}
                        </SourceLink>
                      );
                    }
                    return (
                      <a href={href} target="_blank" rel="noreferrer">
                        {children}
                      </a>
                    );
                  },
                }}
              >
                {linkCitations(content, sources)}
              </Markdown>
            </div>
          )}
          {message.pending && !(message.reasoning && !content) && !message.steps?.some((step) => step.state === "running") && (
            <p className="flex items-center gap-2.5 py-1 text-sm text-zinc-500 dark:text-zinc-400" role="status">
              <span className="flex gap-1" aria-hidden>
                {[0, 150, 300].map((delay) => (
                  <span key={delay} className="bg-brand-500 h-1.5 w-1.5 animate-bounce rounded-full" style={{ animationDelay: `${delay}ms` }} />
                ))}
              </span>
              <span className="shimmer">{message.status ?? "Thinking…"}</span>
            </p>
          )}
          {!message.pending && !content && !message.error && (
            <p className="muted py-1 text-sm">{message.reasoning ? "Stopped while still thinking; no answer was written." : "Stopped before any answer."}</p>
          )}
          {message.error && (
            <div className="mt-2">
              <ErrorNote message={message.error} />
            </div>
          )}
          {message.notice && <p className="mt-2 text-xs text-amber-700 dark:text-amber-400">{message.notice}</p>}
          {!message.pending && message.sources && sources.length === 0 && (
            <p className="muted mt-2 flex items-center gap-1.5 text-xs">
              <Icon d={ICONS.search} className="h-3.5 w-3.5" />
              Nothing relevant was found, so this answer is not based on your books.
            </p>
          )}
          {content && !message.pending && <MediaGallery groups={sources} />}
          {onShowSources && sources.length > 0 && (
            <button
              type="button"
              onClick={() => onShowSources(sources, null)}
              className="mt-3 flex items-center gap-2 rounded-full border border-zinc-200 bg-white py-1.5 pr-3.5 pl-2 text-sm font-medium transition hover:border-zinc-300 active:scale-[0.98] dark:border-zinc-800 dark:bg-zinc-900"
            >
              <span className="flex -space-x-1.5">
                {sources.slice(0, 4).map((source) => (
                  <span key={source.n} className="rounded-md ring-2 ring-white dark:ring-zinc-900">
                    <SourceIcon source={source} size={22} />
                  </span>
                ))}
              </span>
              {sources.length} {sources.length === 1 ? "source" : "sources"}
              <Icon d={ICONS.chevron} className="muted h-3.5 w-3.5" />
            </button>
          )}
          {!message.pending && (
            <div className="mt-2 -ml-1.5 flex items-center gap-0.5">
              {content && (
                <button className={ICON_BUTTON} onClick={copy} title={copied ? "Copied" : "Copy answer"} aria-label="Copy answer">
                  <Icon d={copied ? ICONS.check : ICONS.copy} />
                </button>
              )}
              {content && ttsMode !== "off" && <Listen text={content} mode={ttsMode} />}
              {onRegenerate && (
                <button className={ICON_BUTTON} onClick={onRegenerate} title="Answer again" aria-label="Answer again">
                  <Icon d={ICONS.redo} />
                </button>
              )}
              {message.model && <span className="muted ml-2 truncate text-xs">{shortModel(message.model)}</span>}
            </div>
          )}
        </div>
      </div>
      {sources.length > 0 && !onShowSources && (
        <aside aria-label="Sources">
          <div className="sticky top-4 max-h-[calc(100dvh-15rem)] overflow-y-auto pr-1">
            <h3 className="mb-2 flex items-baseline justify-between px-1 text-sm font-semibold">
              Sources
              <span className="muted text-xs font-normal">{sourceCounts(sources)}</span>
            </h3>
            <ol className="space-y-2">
              {sources.map((source) => (
                <li key={source.n}>
                  <SourceCard source={source} hot={hot === source.n} onHover={setHot} />
                </li>
              ))}
            </ol>
          </div>
        </aside>
      )}
    </div>
  );
}

function groupByAge(chats: Chat[]): [string, Chat[]][] {
  const midnight = new Date().setHours(0, 0, 0, 0) / 1000;
  const groups: [string, Chat[]][] = [
    ["Today", []],
    ["Yesterday", []],
    ["Previous 7 days", []],
    ["Older", []],
  ];
  for (const chat of chats) {
    const age = midnight - chat.updated;
    groups[age <= 0 ? 0 : age <= 86400 ? 1 : age <= 86400 * 7 ? 2 : 3][1].push(chat);
  }
  return groups.filter(([, items]) => items.length);
}

function History({
  chats,
  current,
  onNew,
  onPick,
  onChanged,
  header,
}: {
  chats: Chat[];
  current?: string;
  onNew: () => void;
  onPick: () => void;
  onChanged: (deleted?: string) => void;
  header?: ReactNode;
}) {
  const [filter, setFilter] = useState("");
  const shown = useMemo(() => {
    const words = filter.toLowerCase().split(/\s+/).filter(Boolean);
    return chats.filter((chat) => words.every((word) => chat.title.toLowerCase().includes(word)));
  }, [chats, filter]);

  const rename = async (chat: Chat) => {
    const title = window.prompt("Rename chat", chat.title)?.trim();
    if (!title || title === chat.title) return;
    await api(`/api/ai/chats/${chat.id}`, { method: "PATCH", body: JSON.stringify({ title }) });
    onChanged();
  };
  const remove = async (chat: Chat) => {
    if (!window.confirm(`Delete “${chat.title}”? This cannot be undone.`)) return;
    await api(`/api/ai/chats/${chat.id}`, { method: "DELETE" });
    onChanged(chat.id);
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between px-3 pt-3 pb-1">
        <span className="px-1 text-sm font-semibold">Chats</span>
        {header}
      </div>
      <div className="space-y-2 p-3">
        <button
          className="from-brand-600 hover:from-brand-500 flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r to-sky-600 px-3 py-2 text-sm font-medium text-white shadow-sm transition hover:to-sky-500 hover:shadow"
          onClick={onNew}
        >
          <Icon d={ICONS.plus} />
          New chat
        </button>
        {chats.length > 6 && (
          <label className="flex items-center gap-2 rounded-xl bg-zinc-200/60 px-3 py-1.5 dark:bg-zinc-800">
            <Icon d={ICONS.search} className="muted h-3.5 w-3.5" />
            <input
              type="search"
              className="min-w-0 flex-1 bg-transparent text-sm outline-none"
              placeholder="Search chats"
              aria-label="Search chats"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          </label>
        )}
      </div>
      <nav className="min-h-0 flex-1 overflow-y-auto px-2 pb-3" aria-label="Chat history">
        {chats.length === 0 && <p className="muted px-3 py-2 text-sm">Your chats will appear here.</p>}
        {chats.length > 0 && shown.length === 0 && <p className="muted px-3 py-2 text-sm">No chats match.</p>}
        {groupByAge(shown).map(([label, items]) => (
          <div key={label} className="mb-4">
            <h3 className="muted px-3 pb-1 text-[11px] font-semibold tracking-wider uppercase">{label}</h3>
            <ul>
              {items.map((chat) => (
                <li key={chat.id} className="group relative">
                  <Link
                    to={`/ask/${chat.id}`}
                    onClick={onPick}
                    title={chat.title}
                    aria-current={chat.id === current ? "page" : undefined}
                    className={`block truncate rounded-lg py-1.5 pr-3 pl-3 text-sm transition group-focus-within:pr-16 group-hover:pr-16 [@media(hover:none)]:pr-16 ${
                      chat.id === current
                        ? "bg-white font-medium shadow-sm ring-1 ring-zinc-200 dark:bg-zinc-800 dark:ring-zinc-700"
                        : "text-zinc-700 hover:bg-zinc-200/60 dark:text-zinc-300 dark:hover:bg-zinc-800/60"
                    }`}
                  >
                    {chat.title}
                  </Link>
                  <span className="absolute top-1/2 right-1 hidden -translate-y-1/2 group-focus-within:flex group-hover:flex [@media(hover:none)]:flex">
                    <button className={`${ICON_BUTTON} !h-8 !w-8 sm:!h-7 sm:!w-7`} title="Rename" aria-label={`Rename ${chat.title}`} onClick={() => rename(chat)}>
                      <Icon d={ICONS.edit} className="h-3.5 w-3.5" />
                    </button>
                    <button className={`${ICON_BUTTON} !h-8 !w-8 hover:text-red-600 sm:!h-7 sm:!w-7 dark:hover:text-red-400`} title="Delete" aria-label={`Delete ${chat.title}`} onClick={() => remove(chat)}>
                      <Icon d={ICONS.trash} className="h-3.5 w-3.5" />
                    </button>
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </nav>
      <Link to="/settings#ask-ai" className="muted flex items-center gap-2 border-t border-zinc-200 px-4 py-2.5 text-xs transition hover:text-zinc-900 dark:border-zinc-800 dark:hover:text-zinc-100">
        <Icon d={ICONS.gear} className="h-3.5 w-3.5" />
        Providers, web search and voice
      </Link>
    </div>
  );
}

export default function Ask() {
  const { chatId } = useParams();
  const navigate = useNavigate();
  const touch = useTouch();
  const wide = useWide();
  const [sourcePanel, setSourcePanel] = useState<{ sources: Source[]; hot: number | null } | null>(null);
  useAppScreen();
  const status = useData<Status>("/api/status");
  const books = useData<Book[]>("/api/books");
  const chats = useData<Chat[]>("/api/ai/chats");
  const { groups } = useModels();
  const [messages, setMessages] = useState<Message[]>([]);
  const [text, setText] = useState("");
  const [scope, setScope] = useState("");
  const [web, setWeb] = useState(false);
  const [webHint, setWebHint] = useState(false);
  const [picked, setPicked] = useState(() => {
    try {
      return localStorage.getItem(MODEL_KEY) ?? "";
    } catch {
      return "";
    }
  });
  const [busy, setBusy] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [panel, setPanel] = useState(() => {
    try {
      return localStorage.getItem(PANEL_KEY) !== "closed";
    } catch {
      return true;
    }
  });
  const [problem, setProblem] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  const run = useRef(0); // bumps whenever the chat on screen changes, to drop stale stream updates
  const streaming = useRef<string | null>(null); // id of the chat this tab is streaming into
  const input = useRef<HTMLTextAreaElement>(null);
  const scroller = useRef<HTMLDivElement>(null);

  // Focusing the box on a phone would throw the keyboard up uninvited.
  const focusInput = () => {
    if (!window.matchMedia("(pointer: coarse)").matches) input.current?.focus();
  };
  const remember = (key: string, value: string) => {
    try {
      localStorage.setItem(key, value);
    } catch {
      /* the choice just is not remembered */
    }
  };
  const togglePanel = () => setPanel((open) => (remember(PANEL_KEY, open ? "closed" : "open"), !open));

  useEffect(() => {
    // The first reply of a new chat moves the address to /ask/<id>; that is not a switch.
    if (chatId && chatId === streaming.current) return;
    run.current += 1;
    abort.current?.abort();
    stopSpeaking();
    streaming.current = null;
    setBusy(false);
    setProblem(null);
    setSourcePanel(null);
    setMessages([]);
    if (!chatId) return void focusInput();
    const mine = run.current;
    api<{ messages: Message[] }>(`/api/ai/chats/${chatId}`)
      .then((chat) => mine === run.current && setMessages(chat.messages))
      .catch((reason) => mine === run.current && setProblem(String(reason.message)));
  }, [chatId]);
  // Bring the newest question to the top when it is asked or a chat is opened; the answer
  // then fills in below it without the view jumping on every word.
  useEffect(() => {
    const questions = scroller.current?.querySelectorAll("[data-question]");
    questions?.[questions.length - 1]?.scrollIntoView({ block: "start" });
  }, [messages.length]);
  useEffect(() => () => abort.current?.abort(), []);

  const scopeOptions = useMemo<Option[]>(() => {
    const all = books.data ?? [];
    return [
      { value: "", label: "All books", hint: String(all.length) },
      ...categoryCounts(all).map((c) => ({
        value: `category:${c.value}`,
        label: categoryLabel(c.value),
        hint: String(c.count),
        group: "Categories",
      })),
      ...bookOptions(all, true).map((o) => ({ ...o, value: `book:${o.value}`, hint: undefined, group: `Books · ${o.group}` })),
    ];
  }, [books.data]);

  // The model for the next message: the one picked here if it still exists, else the default.
  const models = useMemo(() => modelOptions(groups ?? []), [groups]);
  // A default whose provider has been turned off gives way to the first model on offer.
  const defaultOff = groups?.some((g) => g.provider.id === status.data?.ai_provider && !g.provider.enabled);
  const fallback = defaultOff ? (models[0]?.value ?? "") : modelKey(status.data?.ai_provider ?? "", status.data?.ai_model ?? "");
  const current = picked && (groups === null || models.some((o) => o.value === picked)) ? picked : fallback;
  const pickerOptions = useMemo(
    () => (current && !models.some((o) => o.value === current) ? [{ value: current, label: shortModel(splitModelKey(current)[1]) }, ...models] : models),
    [models, current],
  );

  const stream = async (body: Record<string, unknown>, seed: (all: Message[]) => Message[]) => {
    const mine = ++run.current;
    const patchLast = (change: (message: Message) => Message) =>
      mine === run.current && setMessages((all) => [...all.slice(0, -1), change(all[all.length - 1])]);
    stopSpeaking();
    setMessages(seed);
    setBusy(true);
    const controller = new AbortController();
    abort.current = controller;
    streaming.current = chatId ?? null;
    const [kind, value] = scope.split(/:(.*)/);
    const [provider, model] = splitModelKey(current);
    try {
      const response = await fetch("/api/ai/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ chat_id: chatId ?? "", [kind || "category"]: value ?? "", provider, model, web: web ? "on" : "auto", ...body }),
        signal: controller.signal,
      });
      if (!response.ok || !response.body) {
        const detail = await response.json().catch(() => null);
        throw new Error(typeof detail?.detail === "string" ? detail.detail : `The server answered HTTP ${response.status}`);
      }
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      for (;;) {
        const { value: chunk, done } = await reader.read();
        if (done) break;
        buffer += chunk;
        let end: number;
        while ((end = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, end);
          buffer = buffer.slice(end + 2);
          const event = /^event: (.*)$/m.exec(block)?.[1];
          const data = /^data: (.*)$/m.exec(block)?.[1];
          if (!event || data === undefined || mine !== run.current) continue;
          const payload = JSON.parse(data);
          if (event === "chat") {
            streaming.current = payload.id;
            if (payload.id !== chatId) navigate(`/ask/${payload.id}`, { replace: true });
            chats.reload();
          } else if (event === "status") patchLast((m) => ({ ...m, status: payload }));
          else if (event === "sources") patchLast((m) => ({ ...m, ...payload }));
          else if (event === "delta") patchLast((m) => ({ ...m, content: m.content + payload }));
          else if (event === "step")
            patchLast((m) => {
              const { index, ...step } = payload;
              const steps = [...(m.steps ?? [])];
              steps[index] = step;
              return { ...m, steps };
            });
          else if (event === "thinking")
            patchLast((m) => ({ ...m, reasoning: (m.reasoning ?? "") + payload, thinkingSince: m.thinkingSince ?? Date.now() }));
          else if (event === "thought") patchLast((m) => ({ ...m, thought_seconds: payload }));
          else if (event === "replace") patchLast((m) => ({ ...m, content: payload }));
          else if (event === "notice") patchLast((m) => ({ ...m, notice: payload }));
          else if (event === "done") patchLast((m) => ({ ...m, model: payload.model }));
          else if (event === "error") patchLast((m) => ({ ...m, error: payload }));
        }
      }
    } catch (reason) {
      if (!controller.signal.aborted) patchLast((m) => ({ ...m, error: String((reason as Error).message) }));
    } finally {
      patchLast((m) => ({ ...m, pending: false, status: undefined }));
      if (mine === run.current) {
        setBusy(false);
        chats.reload();
        focusInput();
      }
    }
  };
  const pendingAnswer: Message = { role: "assistant", content: "", pending: true };
  const ask = (question: string) =>
    stream({ question }, (all) => [...all, { role: "user", content: question }, pendingAnswer]);
  const regenerate = () => stream({ regenerate: true }, (all) => [...all.slice(0, -1), pendingAnswer]);

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    const question = text.trim();
    if (!question || busy) return;
    setText("");
    void ask(question);
  };
  const onKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // On a phone Enter starts a new line, as in every messaging app; the button sends.
    if (event.key === "Enter" && !event.shiftKey && !touch && !event.nativeEvent.isComposing) submit(event);
  };
  const newChat = () => {
    setDrawer(false);
    if (chatId) navigate("/ask");
    else {
      // Already on the blank page: there is no address change to trigger the reset.
      run.current += 1;
      abort.current?.abort();
      streaming.current = null;
      setBusy(false);
      setMessages([]);
    }
    focusInput();
  };
  const historyChanged = (deleted?: string) => {
    chats.reload();
    if (deleted && deleted === chatId) navigate("/ask");
  };

  const ready = !!current;
  const waiting = status.data === null || groups === null;
  const blank = messages.length === 0 && !chatId;
  const title = chats.data?.find((chat) => chat.id === chatId)?.title;
  const ttsMode = status.data?.tts_mode ?? "browser";
  const webReady = !!status.data && status.data.web_mode !== "off";
  const suggestions = useMemo(() => {
    const present = new Set((books.data ?? []).map((b) => b.category));
    return Object.entries(SUGGESTIONS)
      .flatMap(([category, prompts]) => (present.has(category) ? prompts : []))
      .slice(0, 4);
  }, [books.data]);

  const composer = (
    <form onSubmit={submit} className="w-full">
      <div className="composer rounded-[1.75rem] border border-zinc-200 bg-white shadow-[0_2px_12px_-2px_rgb(0_0_0/0.08)] transition dark:border-zinc-700/80 dark:bg-zinc-900">
        <textarea
          ref={input}
          rows={1}
          autoFocus={!touch}
          enterKeyHint={touch ? "enter" : "send"}
          className="block max-h-40 min-h-12 w-full resize-none bg-transparent px-5 pt-3.5 pb-1 sm:max-h-48 sm:min-h-13 sm:pt-4 text-base outline-none [field-sizing:content] placeholder:text-zinc-400"
          placeholder={ready || waiting ? "Ask your library anything" : "Add a provider in Settings to start asking"}
          aria-label="Question"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
          disabled={!ready && !waiting}
        />
        <div className="flex items-center gap-1 px-2.5 pt-1 pb-2.5">
          <Combobox value={scope} options={scopeOptions} placeholder="All books" searchPlaceholder="Search categories and books…" up className={`${PILL} max-w-[7.25rem] sm:max-w-44`} onChange={setScope}>
            <Icon d={ICONS.book} className="h-4 w-4 shrink-0" />
          </Combobox>
          <span className="relative">
            <button
              type="button"
              role="switch"
              aria-checked={web && webReady}
              aria-label="Internet search"
              onClick={() => (webReady ? setWeb(!web) : setWebHint(!webHint))}
              onBlur={() => setTimeout(() => setWebHint(false), 150)}
              title={
                !webReady
                  ? "Internet search is not set up yet"
                  : web
                    ? "On: the internet is searched for every question"
                    : "Off: the internet is searched only when your library has no good match"
              }
              className={`${PILL} ${web && webReady ? "bg-sky-100 text-sky-800 hover:bg-sky-200 dark:bg-sky-950 dark:text-sky-200 dark:hover:bg-sky-900" : ""}`}
            >
              <Icon d={ICONS.globe} className="h-4 w-4 shrink-0" />
              <span className="hidden sm:inline">Internet</span>
            </button>
            {webHint && !webReady && (
              <span role="status" className="card absolute bottom-full left-0 z-30 mb-2 block w-64 p-3 text-xs leading-relaxed shadow-lg">
                Internet search is not set up yet. Choose a search service or a searching model first.
                <Link to="/settings#web-search" className="text-brand-700 dark:text-brand-400 mt-1.5 block font-medium" onMouseDown={(event) => event.preventDefault()}>
                  Set it up in Settings →
                </Link>
              </span>
            )}
          </span>
          <span className="flex-1" />
          {pickerOptions.length > 0 && (
            <Combobox
              value={current}
              options={pickerOptions}
              placeholder="Choose a model"
              searchPlaceholder={`Search ${pickerOptions.length} models…`}
              up
              align="right"
              className={`${PILL} max-w-[8rem] sm:max-w-56`}
              onChange={(key) => (setPicked(key), remember(MODEL_KEY, key))}
            />
          )}
          {busy ? (
            <button
              type="button"
              className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-zinc-900 text-white transition hover:bg-zinc-700 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-white"
              onClick={() => abort.current?.abort()}
              title="Stop"
              aria-label="Stop"
            >
              <span className="h-3 w-3 rounded-sm bg-current" />
            </button>
          ) : (
            <button
              className="from-brand-600 flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-gradient-to-br to-sky-600 text-white shadow-sm transition hover:brightness-110 disabled:from-zinc-200 disabled:to-zinc-200 disabled:text-zinc-400 disabled:shadow-none dark:disabled:from-zinc-800 dark:disabled:to-zinc-800 dark:disabled:text-zinc-600"
              disabled={!text.trim() || !ready}
              title="Send"
              aria-label="Send"
            >
              <Icon d={ICONS.send} className="h-4.5 w-4.5" />
            </button>
          )}
        </div>
      </div>
    </form>
  );
  const setup = !ready && !waiting && (
    <div className="w-full rounded-2xl border border-amber-300 bg-amber-50 px-5 py-4 text-sm dark:border-amber-900 dark:bg-amber-950/40">
      <p className="font-medium">Connect a model to use Ask AI</p>
      <p className="mt-1 text-zinc-600 dark:text-zinc-300">
        Add one or more providers: OpenAI, Google Gemini, Perplexity, OpenRouter, or a local one such as Ollama or LM Studio.
      </p>
      <Link to="/settings#ask-ai" className="btn btn-primary mt-3">
        Open settings
      </Link>
    </div>
  );
  const history = (header?: ReactNode) => (
    <History chats={chats.data ?? []} current={chatId} onNew={newChat} onPick={() => setDrawer(false)} onChanged={historyChanged} header={header} />
  );

  return (
    <div className="flex h-[calc(var(--vvh,100dvh)-3.5rem-env(safe-area-inset-bottom))] kb:h-[var(--vvh,100dvh)] md:h-[var(--vvh,100dvh)]">
      {panel && (
        <aside className="hidden w-64 shrink-0 border-r border-zinc-200 bg-zinc-100/70 lg:block dark:border-zinc-800 dark:bg-zinc-900/50">
          {history(
            <button className={ICON_BUTTON} onClick={togglePanel} title="Hide chats" aria-label="Hide chats">
              <Icon d={ICONS.panel} />
            </button>,
          )}
        </aside>
      )}
      {drawer && (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-label="Chat history">
          <button className="absolute inset-0 bg-black/40 backdrop-blur-[2px]" aria-label="Close history" onClick={() => setDrawer(false)} />
          <div className="absolute inset-y-0 left-0 w-72 max-w-[85vw] bg-zinc-50 shadow-xl dark:bg-zinc-900">{history()}</div>
        </div>
      )}

      {sourcePanel && !wide && <SourcesPanel sources={sourcePanel.sources} hot={sourcePanel.hot} onClose={() => setSourcePanel(null)} />}

      <section className={`relative flex min-w-0 flex-1 flex-col ${blank ? "ask-glow" : ""}`}>
        <header className="relative flex h-12 shrink-0 items-center gap-1 px-2">
          <button className={`${ICON_BUTTON} lg:hidden`} onClick={() => setDrawer(true)} title="Chats" aria-label="Show chats">
            <Icon d={ICONS.menu} />
          </button>
          {!panel && (
            <button className={`${ICON_BUTTON} hidden lg:flex`} onClick={togglePanel} title="Show chats" aria-label="Show chats">
              <Icon d={ICONS.panel} />
            </button>
          )}
          {(!panel || !blank) && (
            <button className={`${ICON_BUTTON} ${panel ? "lg:hidden" : ""}`} onClick={newChat} title="New chat" aria-label="New chat">
              <Icon d={ICONS.plus} />
            </button>
          )}
          <h1 className="min-w-0 flex-1 truncate px-2 text-sm font-medium">{blank ? "" : (title ?? "")}</h1>
        </header>

        {blank ? (
          <div className="relative flex flex-1 flex-col items-center overflow-y-auto px-4 pt-[6vh] pb-6 kb:pt-1 kb:pb-2 sm:justify-center sm:pt-0 sm:pb-[10vh]">
            <div className="w-full max-w-2xl space-y-6 kb:space-y-3 sm:space-y-7">
              <div className="rise text-center">
                <span className="from-brand-500 mx-auto mb-5 flex h-14 w-14 kb:hidden items-center justify-center rounded-2xl bg-gradient-to-br to-sky-500 text-white shadow-lg shadow-teal-500/20">
                  <Icon d={ICONS.spark} className="h-7 w-7" />
                </span>
                <h2 className="text-[2.1rem] leading-tight font-semibold tracking-tight kb:text-2xl sm:text-5xl">
                  Ask your{" "}
                  <span className="from-brand-600 dark:from-brand-400 bg-gradient-to-r to-sky-600 bg-clip-text text-transparent dark:to-sky-400">library</span>
                </h2>
                <p className="muted mx-auto mt-3 max-w-md text-[15px] kb:hidden">
                  Answers written from the articles in your books, with every source one click away.
                </p>
              </div>
              {setup}
              {composer}
              {ready && suggestions.length > 0 && (
                <div className="grid gap-2.5 kb:hidden sm:grid-cols-2">
                  {suggestions.map(([label, prompt]) => (
                    <button
                      key={prompt}
                      className="group rounded-2xl border border-zinc-200 bg-white/70 px-4 py-3 text-left transition hover:-translate-y-0.5 hover:border-zinc-300 hover:bg-white hover:shadow-md dark:border-zinc-800 dark:bg-zinc-900/60 dark:hover:border-zinc-700 dark:hover:bg-zinc-900"
                      onClick={() => void ask(prompt)}
                    >
                      <span className="text-brand-700 dark:text-brand-400 text-[11px] font-semibold tracking-wider uppercase">{label}</span>
                      <span className="mt-0.5 block text-sm text-zinc-700 dark:text-zinc-200">{prompt}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        ) : (
          <>
            <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto">
              <div className={`${COLUMN} space-y-7 pt-2 pb-8 sm:space-y-9 sm:pt-4 sm:pb-10`}>
                <ErrorNote message={problem} />
                {messages.map((message, index) =>
                  message.role === "user" ? (
                    <div key={index} className={`rise scroll-mt-4 ${ROW}`} data-question>
                      <div className="flex justify-end">
                        <div className="max-w-[88%] rounded-3xl rounded-br-lg bg-zinc-200/70 px-4.5 py-2.5 text-base leading-relaxed whitespace-pre-wrap dark:bg-zinc-800">
                          {message.content}
                        </div>
                      </div>
                    </div>
                  ) : (
                    <Answer
                      key={index}
                      message={message}
                      ttsMode={ttsMode}
                      onRegenerate={!busy && index === messages.length - 1 ? regenerate : undefined}
                      onShowSources={wide ? undefined : (sources, hot) => setSourcePanel({ sources, hot })}
                    />
                  ),
                )}
                {/* Room for the newest question to sit at the top while its answer arrives. */}
                {busy && <div className="h-[40vh]" aria-hidden />}
              </div>
            </div>
            <div className={`${COLUMN} shrink-0 pb-2 sm:pb-3`}>
              <div className={ROW}>
                <div className="space-y-2">
                  {setup}
                  {composer}
                  <p className="muted hidden text-center text-xs sm:block">Answers come from a language model and can be wrong. Open the sources to check.</p>
                </div>
              </div>
            </div>
          </>
        )}
      </section>
    </div>
  );
}
