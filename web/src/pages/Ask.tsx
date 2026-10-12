import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import Markdown from "react-markdown";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, authHeaders, readUrl, type Book, type Status } from "../lib/api";
import { useData } from "../lib/live";
import { ScopeBar } from "../Scope";
import { ErrorNote } from "../ui";

interface Source {
  n: number;
  book: string;
  book_title: string;
  path: string;
  title: string;
  excerpt: string;
}
interface Message {
  role: "user" | "assistant";
  content: string;
  sources?: Source[];
  queries?: string[];
  status?: string;
  error?: string;
  pending?: boolean;
}
interface Chat {
  id: string;
  title: string;
  updated: number;
}

/** Answer on the left, its sources in a column on the right (when there is room). */
const ROW = "xl:grid xl:grid-cols-[minmax(0,1fr)_17rem] xl:gap-8";

/** Turn "[2]" in the answer into a link to source 2. */
function linkCitations(text: string, sources: Source[] = []): string {
  return text.replace(/\[(\d+)\](?!\()/g, (whole, n) => {
    const source = sources.find((s) => s.n === Number(n));
    return source ? `[\\[${n}\\]](${readUrl(source.book, source.path)})` : whole;
  });
}

function SourceNumber({ n }: { n: number }) {
  return (
    <span className="bg-brand-100 text-brand-700 dark:bg-brand-900 dark:text-brand-100 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs font-semibold">
      {n}
    </span>
  );
}

function Answer({ message }: { message: Message }) {
  const sources = message.sources ?? [];
  return (
    <div className={ROW}>
      <div className="min-w-0">
        {sources.length > 0 && (
          <div className="mb-3 flex gap-1.5 overflow-x-auto pb-1 [scrollbar-width:none] xl:hidden">
            {sources.map((source) => (
              <Link
                key={source.n}
                to={readUrl(source.book, source.path)}
                className="card hover:border-brand-500 flex w-52 shrink-0 items-center gap-2 px-2.5 py-1.5 text-xs transition"
              >
                <SourceNumber n={source.n} />
                <span className="min-w-0">
                  <span className="block truncate font-medium">{source.title}</span>
                  <span className="muted block truncate">{source.book_title}</span>
                </span>
              </Link>
            ))}
          </div>
        )}
        {message.sources && sources.length === 0 && (
          <p className="muted mb-3 text-xs">
            Nothing relevant was found in the library
            {message.queries?.length ? ` for ${message.queries.map((q) => `“${q}”`).join(", ")}` : ""}.
          </p>
        )}
        {message.content && (
          <div className="answer text-[15px] leading-relaxed">
            <Markdown
              components={{
                a: ({ href = "", children }) =>
                  href.startsWith("/read/") ? (
                    <Link to={href} className="text-brand-700 dark:text-brand-400 text-xs font-medium no-underline hover:underline">
                      {children}
                    </Link>
                  ) : (
                    <a href={href} target="_blank" rel="noreferrer">
                      {children}
                    </a>
                  ),
              }}
            >
              {linkCitations(message.content, sources)}
            </Markdown>
          </div>
        )}
        {message.pending && (
          <p className="muted mt-2 flex items-center gap-2 text-sm">
            <span className="bg-brand-500 inline-block h-2 w-2 animate-pulse rounded-full" />
            {message.status ?? "Thinking…"}
          </p>
        )}
        {!message.pending && !message.content && !message.error && <p className="muted text-sm">Stopped before any answer.</p>}
        {message.error && (
          <div className="mt-2">
            <ErrorNote message={message.error} />
          </div>
        )}
      </div>
      {sources.length > 0 && (
        <aside className="hidden xl:block" aria-label="Sources">
          <div className="sticky top-6">
            <h3 className="muted mb-2 text-xs font-semibold tracking-wide uppercase">Sources</h3>
            <ol className="space-y-2">
              {sources.map((source) => (
                <li key={source.n}>
                  <Link
                    to={readUrl(source.book, source.path)}
                    className="card hover:border-brand-500 block p-3 transition hover:shadow-sm"
                  >
                    <span className="flex items-center gap-2">
                      <SourceNumber n={source.n} />
                      <span className="muted truncate text-xs">{source.book_title}</span>
                    </span>
                    <span className="mt-1.5 line-clamp-2 text-sm leading-snug font-medium">{source.title}</span>
                    <span className="muted mt-1 line-clamp-2 text-xs">{source.excerpt}</span>
                  </Link>
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
    ["Last 7 days", []],
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
}: {
  chats: Chat[];
  current?: string;
  onNew: () => void;
  onPick: () => void;
  onChanged: (deleted?: string) => void;
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
    <div className="flex h-full flex-col gap-3">
      <button className="btn btn-primary w-full" onClick={onNew}>
        <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
          <path d="M8 3v10M3 8h10" strokeLinecap="round" />
        </svg>
        New chat
      </button>
      {chats.length > 6 && (
        <input
          type="search"
          className="input"
          placeholder="Search chats…"
          aria-label="Search chats"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
      )}
      <nav className="-mx-1 min-h-0 flex-1 overflow-y-auto px-1" aria-label="Chat history">
        {chats.length === 0 && <p className="muted px-2 py-3 text-sm">Your chats will be listed here.</p>}
        {chats.length > 0 && shown.length === 0 && <p className="muted px-2 py-3 text-sm">No chats match.</p>}
        {groupByAge(shown).map(([label, items]) => (
          <div key={label} className="mb-3">
            <h3 className="muted px-2 pb-1 text-xs font-semibold tracking-wide uppercase">{label}</h3>
            <ul>
              {items.map((chat) => (
                <li key={chat.id} className="group relative">
                  <Link
                    to={`/ask/${chat.id}`}
                    onClick={onPick}
                    title={chat.title}
                    aria-current={chat.id === current ? "page" : undefined}
                    className={`block truncate rounded-lg py-1.5 pr-14 pl-2 text-sm ${
                      chat.id === current
                        ? "bg-brand-50 text-brand-700 dark:bg-brand-900/50 dark:text-brand-100 font-medium"
                        : "text-zinc-700 hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-800"
                    }`}
                  >
                    {chat.title}
                  </Link>
                  <span className="absolute top-1/2 right-1 flex -translate-y-1/2 opacity-0 group-focus-within:opacity-100 group-hover:opacity-100 [@media(hover:none)]:opacity-100">
                    <button className="muted rounded p-1 hover:text-zinc-900 dark:hover:text-zinc-100" title="Rename" aria-label={`Rename ${chat.title}`} onClick={() => rename(chat)}>
                      <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
                        <path d="m10.5 2.5 3 3L6 13l-3.5.5L3 10z" strokeLinejoin="round" />
                      </svg>
                    </button>
                    <button className="muted rounded p-1 hover:text-red-600" title="Delete" aria-label={`Delete ${chat.title}`} onClick={() => remove(chat)}>
                      <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
                        <path d="M3 4.5h10M6.5 4.5v-2h3v2M4.5 4.5l.5 9h6l.5-9" strokeLinecap="round" strokeLinejoin="round" />
                      </svg>
                    </button>
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </nav>
    </div>
  );
}

export default function Ask() {
  const { chatId } = useParams();
  const navigate = useNavigate();
  const status = useData<Status>("/api/status");
  const books = useData<Book[]>("/api/books");
  const chats = useData<Chat[]>("/api/ai/chats");
  const [messages, setMessages] = useState<Message[]>([]);
  const [text, setText] = useState("");
  const [scope, setScope] = useState({ category: "", book: "" });
  const [busy, setBusy] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  const run = useRef(0); // bumps whenever the chat on screen changes, to drop stale stream updates
  const streaming = useRef<string | null>(null); // id of the chat this tab is streaming into
  const input = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    // The first reply of a new chat moves the address to /ask/<id>; that is not a switch.
    if (chatId && chatId === streaming.current) return;
    run.current += 1;
    abort.current?.abort();
    streaming.current = null;
    setBusy(false);
    setProblem(null);
    setMessages([]);
    if (!chatId) return void input.current?.focus();
    const mine = run.current;
    api<{ messages: Message[] }>(`/api/ai/chats/${chatId}`)
      .then((chat) => mine === run.current && setMessages(chat.messages))
      .catch((reason) => mine === run.current && setProblem(String(reason.message)));
  }, [chatId]);
  // Bring the newest question to the top when it is asked or a chat is opened; the answer
  // then fills in below it without the page jumping on every word.
  useEffect(() => {
    const questions = document.querySelectorAll("[data-question]");
    questions[questions.length - 1]?.scrollIntoView({ block: "start" });
  }, [messages.length]);
  useEffect(() => () => abort.current?.abort(), []);

  const ask = async (question: string) => {
    const mine = ++run.current;
    const patchLast = (change: (message: Message) => Message) =>
      mine === run.current && setMessages((all) => [...all.slice(0, -1), change(all[all.length - 1])]);
    setMessages((all) => [...all, { role: "user", content: question }, { role: "assistant", content: "", pending: true }]);
    setBusy(true);
    const controller = new AbortController();
    abort.current = controller;
    streaming.current = chatId ?? null;
    try {
      const response = await fetch("/api/ai/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ question, chat_id: chatId ?? "", ...scope }),
        signal: controller.signal,
      });
      if (!response.ok || !response.body) {
        const detail = await response.json().catch(() => null);
        throw new Error(detail?.detail ?? `The server answered HTTP ${response.status}`);
      }
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += value;
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
        input.current?.focus();
      }
    }
  };

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    const question = text.trim();
    if (!question || busy) return;
    setText("");
    void ask(question);
  };
  const onKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) submit(event);
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
    input.current?.focus();
  };
  const historyChanged = (deleted?: string) => {
    chats.reload();
    if (deleted && deleted === chatId) navigate("/ask");
  };

  const configured = status.data?.ai_configured;
  const scopeBook = books.data?.find((b) => b.id === scope.book);
  const composer = (
    <form onSubmit={submit} className="w-full">
      <div className="focus-within:border-brand-500 focus-within:ring-brand-500/30 flex items-end gap-2 rounded-3xl border border-zinc-300 bg-white py-2 pr-2 pl-5 shadow-sm transition focus-within:ring-2 dark:border-zinc-700 dark:bg-zinc-900">
        <textarea
          ref={input}
          rows={1}
          autoFocus
          className="max-h-40 min-h-9 min-w-0 flex-1 resize-none bg-transparent py-1.5 outline-none [field-sizing:content]"
          placeholder={scopeBook ? `Ask ${scopeBook.title}…` : "Ask your library anything…"}
          aria-label="Question"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
          disabled={configured === false}
        />
        {busy ? (
          <button type="button" className="btn rounded-full" onClick={() => abort.current?.abort()}>
            Stop
          </button>
        ) : (
          <button className="btn btn-primary rounded-full" disabled={!text.trim() || configured === false}>
            Ask
          </button>
        )}
      </div>
    </form>
  );
  const scopeBar = (up: boolean) => (
    <ScopeBar books={books.data ?? []} category={scope.category} book={scope.book} onChange={setScope} center up={up} />
  );
  const setup = configured === false && (
    <div className="card w-full px-5 py-4 text-sm">
      <p className="font-medium">Connect a model to use Ask AI</p>
      <p className="muted mt-1">
        Any OpenAI-compatible server works: OpenAI, OpenRouter, or a local one such as Ollama, LM Studio or llama.cpp.
      </p>
      <Link to="/settings#ask-ai" className="btn btn-primary mt-3">
        Open settings
      </Link>
    </div>
  );
  const history = (
    <History chats={chats.data ?? []} current={chatId} onNew={newChat} onPick={() => setDrawer(false)} onChanged={historyChanged} />
  );
  const blank = messages.length === 0 && !chatId;

  return (
    <div className="lg:grid lg:grid-cols-[15rem_minmax(0,1fr)] lg:gap-8">
      <aside className="hidden lg:block">
        <div className="sticky top-6 h-[calc(100vh-3rem)]">{history}</div>
      </aside>
      {drawer && (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-label="Chat history">
          <button className="absolute inset-0 bg-black/40" aria-label="Close history" onClick={() => setDrawer(false)} />
          <div className="absolute inset-y-0 left-0 w-72 max-w-[85vw] bg-white p-4 shadow-xl dark:bg-zinc-900">{history}</div>
        </div>
      )}

      <section className="flex min-h-[calc(100vh-7rem)] min-w-0 flex-col md:min-h-[calc(100vh-3rem)]">
        <div className="mb-4 flex items-center justify-between gap-2 lg:hidden">
          <button className="btn" onClick={() => setDrawer(true)}>
            <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
              <path d="M2.5 4h11M2.5 8h11M2.5 12h11" strokeLinecap="round" />
            </svg>
            History
          </button>
          {!blank && (
            <button className="btn" onClick={newChat}>
              New chat
            </button>
          )}
        </div>
        <ErrorNote message={problem} />

        {blank ? (
          <div className="mx-auto flex w-full max-w-2xl flex-1 flex-col items-center justify-center gap-5 pb-24">
            <h1 className="text-center text-4xl font-semibold tracking-tight sm:text-5xl">Ask your library</h1>
            <p className="muted -mt-2 text-center text-sm">
              Answers are written by your model from articles found in your books, with the sources cited.
            </p>
            {setup}
            {composer}
            {scopeBar(false)}
          </div>
        ) : (
          <>
            <div className="flex-1 space-y-7 pb-6">
              {messages.map((message, index) =>
                message.role === "user" ? (
                  <div key={index} className={`scroll-mt-16 md:scroll-mt-6 ${ROW}`} data-question>
                    <div className="flex justify-end">
                      <div className="bg-brand-700 dark:bg-brand-600 max-w-[85%] rounded-2xl rounded-br-md px-4 py-2 whitespace-pre-wrap text-white">
                        {message.content}
                      </div>
                    </div>
                  </div>
                ) : (
                  <Answer key={index} message={message} />
                ),
              )}
            </div>
            <div className={`sticky bottom-0 -mx-4 bg-zinc-50 px-4 pt-2 pb-4 dark:bg-zinc-950 ${ROW}`}>
              <div className="space-y-2.5">
                {setup}
                {composer}
                {scopeBar(true)}
              </div>
            </div>
          </>
        )}
      </section>
    </div>
  );
}
