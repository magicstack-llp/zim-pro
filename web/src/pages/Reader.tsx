import { useEffect, useRef, useState, type FormEvent } from "react";
import ReactMarkdown from "react-markdown";
import { Link, useNavigate, useParams } from "react-router-dom";
import remarkGfm from "remark-gfm";
import { api, contentUrl, markdownUrl, readUrl, type Book } from "../lib/api";
import { BookIcon } from "../ui";
import { bookIcon } from "./Library";

interface Suggestion {
  path: string;
  title: string;
}

export default function Reader() {
  const { bookId = "", "*": initialPath = "" } = useParams();
  const navigate = useNavigate();
  const frame = useRef<HTMLIFrameElement>(null);
  const [book, setBook] = useState<Book | null>(null);
  const [missing, setMissing] = useState(false);
  // The iframe navigates on its own; `src` is only set when we send it somewhere.
  const [src, setSrc] = useState(() => contentUrl(bookId, initialPath));
  const [query, setQuery] = useState("");
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [open, setOpen] = useState(false);
  // The article the iframe is showing, and its text as Markdown when that view is on.
  const [path, setPath] = useState(initialPath);
  const [asMarkdown, setAsMarkdown] = useState(false);
  const [markdown, setMarkdown] = useState<string | null>(null);
  const [markdownError, setMarkdownError] = useState("");
  const [copied, setCopied] = useState(false);
  const markdownPane = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!asMarkdown) return;
    let current = true;
    setMarkdown(null);
    setMarkdownError("");
    fetch(markdownUrl(bookId, path), { headers: { Accept: "text/markdown" } })
      .then(async (reply) => {
        if (!reply.ok) throw new Error(reply.status === 415 ? "This page is not text." : "This page could not be converted.");
        return reply.text();
      })
      .then((text) => {
        if (!current) return;
        setMarkdown(text);
        markdownPane.current?.scrollTo(0, 0);
      })
      .catch((error: Error) => current && setMarkdownError(error.message));
    return () => {
      current = false;
    };
  }, [asMarkdown, bookId, path]);

  const copyMarkdown = async () => {
    if (!markdown) return;
    try {
      await navigator.clipboard.writeText(markdown);
    } catch {
      // No clipboard API on plain http from another machine: copy through a text box.
      const box = document.createElement("textarea");
      box.value = markdown;
      box.style.position = "fixed";
      box.style.opacity = "0";
      document.body.append(box);
      box.select();
      document.execCommand("copy");
      box.remove();
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  useEffect(() => {
    setMissing(false);
    api<Book>(`/api/books/${encodeURIComponent(bookId)}`)
      .then((found) => {
        setBook(found);
        document.title = `${found.title} · Zim Pro`;
      })
      .catch(() => setMissing(true));
    return () => {
      document.title = "Zim Pro";
    };
  }, [bookId]);

  useEffect(() => {
    if (query.trim().length < 2) return setSuggestions([]);
    const timer = setTimeout(() => {
      api<Suggestion[]>(`/api/suggest?book=${encodeURIComponent(bookId)}&q=${encodeURIComponent(query)}`)
        .then(setSuggestions)
        .catch(() => setSuggestions([]));
    }, 150);
    return () => clearTimeout(timer);
  }, [query, bookId]);

  const go = (url: string) => {
    // Replace, don't assign: changing the src attribute to its current value does nothing.
    if (frame.current?.contentWindow) frame.current.contentWindow.location.replace(url);
    else setSrc(url);
    setOpen(false);
  };

  // Mirror the article shown in the iframe into the address bar, so it can be bookmarked.
  const syncLocation = () => {
    try {
      const inner = frame.current?.contentWindow?.location;
      const prefix = `/content/${encodeURIComponent(bookId)}/`;
      if (inner && inner.pathname.startsWith(prefix)) {
        const shown = decodeURIComponent(inner.pathname.slice(prefix.length));
        setPath(shown);
        window.history.replaceState(window.history.state, "", readUrl(bookId, shown) + inner.search + inner.hash);
      }
    } catch {
      /* the frame left this origin */
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (query.trim()) navigate(`/search?book=${encodeURIComponent(bookId)}&q=${encodeURIComponent(query.trim())}`);
  };

  if (missing) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3">
        <p className="font-medium">This book is not in the library.</p>
        <Link className="btn" to="/">
          Back to library
        </Link>
      </div>
    );
  }

  return (
    <div className="flex h-[var(--vvh,100dvh)] flex-col">
      <header className="flex shrink-0 items-center gap-2 border-b border-zinc-200 bg-white px-2 py-1.5 dark:border-zinc-800 dark:bg-zinc-900">
        <Link to="/" className="btn px-2" title="Back to library">
          ←<span className="hidden sm:inline"> Library</span>
        </Link>
        {book && <BookIcon src={bookIcon(book)} title={book.title} size={28} />}
        <span className="hidden max-w-48 truncate text-sm font-medium lg:block">{book?.title}</span>
        <form onSubmit={submit} className="relative mx-auto w-full max-w-xl">
          <input
            className="input"
            placeholder={book ? `Search ${book.title}…` : "Search…"}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setOpen(true);
            }}
            onFocus={() => setOpen(true)}
            onBlur={() => setTimeout(() => setOpen(false), 150)}
          />
          {open && suggestions.length > 0 && (
            <ul className="card absolute top-full right-0 left-0 z-30 mt-1 max-h-80 overflow-auto py-1 shadow-lg">
              {suggestions.map((item) => (
                <li key={item.path}>
                  <button
                    type="button"
                    className="block w-full truncate px-3 py-1.5 text-left text-sm hover:bg-zinc-100 dark:hover:bg-zinc-800"
                    onMouseDown={() => {
                      setQuery("");
                      go(contentUrl(bookId, item.path));
                    }}
                  >
                    {item.title}
                  </button>
                </li>
              ))}
              <li className="border-t border-zinc-200 dark:border-zinc-800">
                <button type="submit" className="text-brand-600 block w-full px-3 py-1.5 text-left text-sm">
                  Search full text for “{query}”
                </button>
              </li>
            </ul>
          )}
        </form>
        <button className="btn px-2.5" title="Main page" aria-label="Main page" onClick={() => go(contentUrl(bookId))}>
          <svg viewBox="0 0 20 20" className="h-4.5 w-4.5 sm:hidden" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <path d="M3.5 9.5 10 4l6.5 5.5M5.5 8.5V16h9V8.5" />
          </svg>
          <span className="hidden sm:inline">Home</span>
        </button>
        <button
          className="btn px-2"
          title="Random article"
          aria-label="Random article"
          onClick={() => go(`/random/${encodeURIComponent(bookId)}?t=${Date.now()}`)}
        >
          <svg viewBox="0 0 20 20" className="h-4.5 w-4.5 sm:hidden" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <path d="M3 6h3l7 8h4M3 14h3l2-2.3M12 8.3 13 6h4M15 4l2 2-2 2M15 12l2 2-2 2" />
          </svg>
          <span className="hidden sm:inline">Random</span>
        </button>
        <button
          className={`btn px-2 ${asMarkdown ? "border-brand-500 text-brand-700 dark:text-brand-300" : ""}`}
          title={asMarkdown ? "Show the page as it was published" : "Show this page as Markdown"}
          aria-pressed={asMarkdown}
          onClick={() => setAsMarkdown(!asMarkdown)}
        >
          <span className="sm:hidden">MD</span>
          <span className="hidden sm:inline">Markdown</span>
        </button>
        <a
          className="btn hidden px-2 sm:inline-flex"
          title="Open this page without the toolbar"
          href={src}
          onClick={(e) => {
            try {
              e.currentTarget.href = frame.current!.contentWindow!.location.href;
            } catch {
              /* keep the last known address */
            }
          }}
          target="_blank"
          rel="noreferrer"
        >
          ↗
        </a>
      </header>
      <iframe
        ref={frame}
        src={src}
        onLoad={syncLocation}
        title={book?.title ?? "Article"}
        className={`min-h-0 w-full flex-1 border-0 bg-white ${asMarkdown ? "hidden" : ""}`}
      />
      {asMarkdown && (
        <div ref={markdownPane} className="min-h-0 flex-1 overflow-auto">
          <div className="mx-auto max-w-3xl px-4 pt-3 pb-16">
            <div className="mb-4 flex items-center gap-2">
              <span className="muted mr-auto text-xs">
                {markdown ? `${markdown.length.toLocaleString()} characters` : ""}
              </span>
              <button className="btn" onClick={copyMarkdown} disabled={!markdown}>
                {copied ? "Copied" : "Copy Markdown"}
              </button>
              <a className="btn" href={markdownUrl(bookId, path)} target="_blank" rel="noreferrer">
                Raw
              </a>
            </div>
            {markdownError ? (
              <p className="muted">{markdownError}</p>
            ) : markdown === null ? (
              <p className="muted">Converting…</p>
            ) : (
              <div className="answer">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={{
                    // Spans, not a figure: Markdown puts pictures inside paragraphs.
                    img: ({ src, alt }) => (
                      <span className="my-4 block">
                        <img src={src} alt={alt ?? ""} loading="lazy" className="max-h-[70vh] max-w-full rounded-lg bg-white" />
                        {alt && <span className="muted mt-1 block text-sm">{alt}</span>}
                      </span>
                    ),
                    a: ({ href = "", children }) =>
                      /\.(webm|mp4|m4v|ogv)$/i.test(href) ? (
                        <video src={href} controls preload="metadata" className="my-4 block max-h-[70vh] w-full rounded-lg bg-black" />
                      ) : /\.(mp3|ogg|oga|opus|m4a|wav)$/i.test(href) ? (
                        <audio src={href} controls preload="metadata" className="my-4 block w-full" />
                      ) : href.startsWith("/markdown/") ? (
                        // A link to another article: load it in the reader, which
                        // brings its Markdown here.
                        <a
                          href={href}
                          onClick={(event) => {
                            event.preventDefault();
                            go(href.replace("/markdown/", "/content/"));
                          }}
                        >
                          {children}
                        </a>
                      ) : (
                        <a href={href} target="_blank" rel="noreferrer">
                          {children}
                        </a>
                      ),
                  }}
                >
                  {markdown}
                </ReactMarkdown>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
