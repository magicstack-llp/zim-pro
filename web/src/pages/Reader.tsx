import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, contentUrl, readUrl, type Book } from "../lib/api";
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

  useEffect(() => {
    setMissing(false);
    api<Book>(`/api/books/${encodeURIComponent(bookId)}`)
      .then((found) => {
        setBook(found);
        document.title = `${found.title} · Kiwix Library`;
      })
      .catch(() => setMissing(true));
    return () => {
      document.title = "Kiwix Library";
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
        const path = decodeURIComponent(inner.pathname.slice(prefix.length));
        window.history.replaceState(window.history.state, "", readUrl(bookId, path) + inner.search + inner.hash);
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
    <div className="flex h-full flex-col">
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
        <button className="btn px-2" title="Main page" onClick={() => go(contentUrl(bookId))}>
          Home
        </button>
        <button
          className="btn px-2"
          title="Random article"
          onClick={() => go(`/random/${encodeURIComponent(bookId)}?t=${Date.now()}`)}
        >
          Random
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
        className="min-h-0 w-full flex-1 border-0 bg-white"
      />
    </div>
  );
}
