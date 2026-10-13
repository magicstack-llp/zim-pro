import { useEffect, useRef, useState } from "react";
import { Dialog } from "./Dialog";
import { api, authHeaders, post } from "./lib/api";
import { categoryLabel } from "./lib/format";
import { useData } from "./lib/live";
import { BookIcon, ErrorNote } from "./ui";

/** How a ZIM made here is filed in the library, and the picture it is shown with. */

export type SourceKind = "link" | "upload" | "feed";
interface Category {
  name: string;
  /** How many links, uploads and feed collections are filed under it. */
  used: number;
}

/** A category the library already has, from the books in it. */
interface LibraryCategory {
  name: string;
  books: number;
}
type Listing = { categories: Category[]; library?: LibraryCategory[] };

const NEW = "\u0000new";

/** The picture to show for a source: the one chosen for it, else its ZIM's own. */
type Source = { id?: number; icon?: number | null; book_id?: string; book_title?: string | null };
/** The picture the user chose for a source, if they chose one. */
export const ownIcon = (kind: SourceKind, source: Source) => (source.id && source.icon ? `/api/icons/${kind}/${source.id}?v=${Math.round(source.icon)}` : null);
/** The picture of the ZIM made from a source, once there is one. */
export const bookIconOf = (source: Source) => (source.book_id && source.book_title != null ? `/api/books/${encodeURIComponent(source.book_id)}/illustration` : null);
export const sourceIcon = (kind: SourceKind, source: Source) => ownIcon(kind, source) ?? bookIconOf(source);

/** Picking a category, with a way to add one on the spot and to tidy the list. */
export function CategorySelect({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const { data, reload } = useData<Listing>("/api/categories");
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [managing, setManaging] = useState(false);
  const categories = data?.categories ?? [];
  const library = data?.library ?? [];
  // A category the lists have not caught up with yet, such as one just typed in a CSV.
  const known = [...categories, ...library].some((c) => c.name.toLowerCase() === value.toLowerCase());
  const names = known || !value ? categories.map((c) => c.name) : [...categories.map((c) => c.name), value];
  const add = () => {
    const wanted = name.trim();
    if (!wanted) return;
    setProblem(null);
    post<Listing>("/api/categories", { name: wanted })
      .then((result) => {
        reload();
        onChange(result.categories.find((c) => c.name.toLowerCase() === wanted.toLowerCase())?.name ?? wanted);
        setAdding(false);
        setName("");
      })
      .catch((reason) => setProblem(String(reason.message)));
  };
  return (
    <div className="w-full sm:w-auto">
      {adding ? (
        <div className="flex flex-wrap items-center gap-2">
          <input
            className="input w-full sm:w-48"
            placeholder="Name of the category"
            value={name}
            maxLength={40}
            autoFocus
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                add();
              }
              if (e.key === "Escape") {
                e.stopPropagation();
                setAdding(false);
              }
            }}
            aria-label="New category"
          />
          <button type="button" className="btn btn-primary" onClick={add} disabled={!name.trim()}>
            Add
          </button>
          <button type="button" className="btn" onClick={() => (setAdding(false), setProblem(null))}>
            Cancel
          </button>
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <select className="input w-full min-w-0 sm:w-56" value={value} onChange={(e) => (e.target.value === NEW ? setAdding(true) : onChange(e.target.value))} aria-label="Category">
            <option value="">My ZIMs (no category)</option>
            {names.length > 0 && (
              <optgroup label="Your categories">
                {names.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </optgroup>
            )}
            {library.length > 0 && (
              <optgroup label="Already in your library">
                {library.map((item) => (
                  <option key={item.name} value={item.name}>
                    {categoryLabel(item.name)} ({item.books})
                  </option>
                ))}
              </optgroup>
            )}
            <option value={NEW}>+ New category…</option>
          </select>
          <button type="button" className="btn shrink-0" onClick={() => setManaging(true)}>
            Manage
          </button>
        </div>
      )}
      {problem && <p className="mt-1 text-xs text-red-600 dark:text-red-400">{problem}</p>}
      {managing && (
        <CategoriesDialog
          onClose={() => {
            setManaging(false);
            reload();
          }}
          onRenamed={(from, to) => value.toLowerCase() === from.toLowerCase() && onChange(to)}
        />
      )}
    </div>
  );
}

/** The list of categories: add, rename and remove. */
export function CategoriesDialog({ onClose, onRenamed }: { onClose: () => void; onRenamed?: (from: string, to: string) => void }) {
  const [categories, setCategories] = useState<Category[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [editing, setEditing] = useState<{ from: string; to: string } | null>(null);
  useEffect(() => {
    api<{ categories: Category[] }>("/api/categories")
      .then((result) => setCategories(result.categories))
      .catch((reason) => setProblem(String(reason.message)));
  }, []);
  const run = (request: Promise<{ categories: Category[] }>, then?: () => void) => {
    setProblem(null);
    request
      .then((result) => {
        setCategories(result.categories);
        then?.();
      })
      .catch((reason) => setProblem(String(reason.message)));
  };
  const address = (item: string) => `/api/categories/${encodeURIComponent(item)}`;
  const rename = () => {
    if (!editing) return;
    const { from, to } = editing;
    if (!to.trim() || to.trim() === from) return setEditing(null);
    run(api(address(from), { method: "PUT", body: JSON.stringify({ name: to }) }), () => {
      onRenamed?.(from, to.trim());
      setEditing(null);
    });
  };
  const remove = (item: Category) => {
    const warning = item.used ? `Remove “${item.name}”? The ${item.used} ${item.used === 1 ? "ZIM" : "ZIMs"} filed under it go back to My ZIMs.` : `Remove “${item.name}”?`;
    if (window.confirm(warning))
      run(api(address(item.name), { method: "DELETE" }), () => onRenamed?.(item.name, ""));
  };
  return (
    <Dialog
      title="Categories"
      onClose={onClose}
      footer={
        <button className="btn btn-primary" onClick={onClose}>
          Done
        </button>
      }
    >
      <ErrorNote message={problem} />
      <p className="muted text-sm">Categories group the ZIMs you make. They show as filters in the Library, in Search and in Ask AI.</p>
      <form
        className="mt-3 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (name.trim()) run(post("/api/categories", { name }), () => setName(""));
        }}
      >
        <input className="input min-w-0 flex-1" placeholder="New category" value={name} maxLength={40} onChange={(e) => setName(e.target.value)} aria-label="New category name" />
        <button className="btn btn-primary shrink-0" disabled={!name.trim()}>
          Add
        </button>
      </form>
      {categories && categories.length === 0 && <p className="muted mt-4 text-center text-sm">No categories yet. Everything you make is filed under My ZIMs.</p>}
      <ul className="mt-3 divide-y divide-zinc-100 dark:divide-zinc-800">
        {(categories ?? []).map((item) => (
          <li key={item.name} className="flex flex-wrap items-center gap-2 py-2.5">
            {editing?.from === item.name ? (
              <>
                <input
                  className="input min-w-0 flex-1"
                  value={editing.to}
                  maxLength={40}
                  autoFocus
                  onChange={(e) => setEditing({ from: item.name, to: e.target.value })}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") rename();
                    if (e.key === "Escape") {
                      e.stopPropagation();
                      setEditing(null);
                    }
                  }}
                  aria-label={`New name for ${item.name}`}
                />
                <button className="btn btn-primary" onClick={rename}>
                  Save
                </button>
                <button className="btn" onClick={() => setEditing(null)}>
                  Cancel
                </button>
              </>
            ) : (
              <>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium">{item.name}</div>
                  <div className="muted text-xs">{item.used ? `${item.used} ${item.used === 1 ? "ZIM" : "ZIMs"}` : "Not used yet"}</div>
                </div>
                <button className="btn" onClick={() => setEditing({ from: item.name, to: item.name })}>
                  Rename
                </button>
                <button className="btn btn-danger" onClick={() => remove(item)}>
                  Remove
                </button>
              </>
            )}
          </li>
        ))}
      </ul>
    </Dialog>
  );
}

/** A picture waiting to be saved with the form it was chosen in. */
export interface IconChoice {
  file: File | null;
  /** The picture chosen before is to be taken away. */
  removed: boolean;
}
export const NO_ICON_CHANGE: IconChoice = { file: null, removed: false };

/** Send what was chosen in an {@link IconPicker}, once the source it belongs to is saved. */
export async function saveIcon(kind: SourceKind, id: number, choice: IconChoice): Promise<void> {
  if (choice.file) {
    const form = new FormData();
    form.append("file", choice.file);
    const reply = await fetch(`/api/icons/${kind}/${id}`, { method: "POST", body: form, headers: authHeaders() });
    if (!reply.ok) {
      const detail = await reply.json().then((body) => body.detail).catch(() => null);
      throw new Error(typeof detail === "string" ? detail : "The picture could not be saved.");
    }
  } else if (choice.removed) {
    await api(`/api/icons/${kind}/${id}`, { method: "DELETE" });
  }
}

/** Choosing the picture a ZIM is shown with; nothing is sent until the form is saved. */
export function IconPicker({
  own,
  fallback = null,
  title,
  choice,
  onChange,
  hint,
}: {
  /** The picture chosen before, if any. */
  own: string | null;
  /** What is shown without one: the picture the ZIM came with. */
  fallback?: string | null;
  title: string;
  choice: IconChoice;
  onChange: (choice: IconChoice) => void;
  hint: string;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [preview, setPreview] = useState<string | null>(null);
  useEffect(() => {
    if (!choice.file) return setPreview(null);
    const url = URL.createObjectURL(choice.file);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [choice.file]);
  const chosen = preview ?? (choice.removed ? null : own);
  const shown = chosen ?? fallback;
  return (
    <div className="flex items-center gap-3">
      <BookIcon key={shown ?? "none"} src={shown} title={title || "Z"} size={48} />
      <div className="min-w-0">
        <div className="flex flex-wrap gap-2">
          <button type="button" className="btn" onClick={() => input.current?.click()}>
            {chosen ? "Change picture" : "Choose picture"}
          </button>
          {chosen && (
            <button type="button" className="btn" onClick={() => onChange({ file: null, removed: true })}>
              Remove
            </button>
          )}
        </div>
        <p className="muted mt-1 text-xs">{choice.file ? "Yours: it goes into the ZIM the next time the ZIM is made." : hint}</p>
      </div>
      <input
        ref={input}
        type="file"
        accept="image/png,image/jpeg,image/webp,image/gif,image/x-icon,.ico"
        className="hidden"
        aria-label="Picture file"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) onChange({ file, removed: false });
          e.target.value = "";
        }}
      />
    </div>
  );
}
