import { useMemo } from "react";
import { Combobox, type Option } from "./Combobox";
import type { Book } from "./lib/api";
import { categoryCounts, categoryLabel } from "./lib/format";

const CHIP =
  "inline-flex max-w-56 items-center gap-1.5 rounded-full border px-3 py-1 text-sm whitespace-nowrap transition";
const CHIP_OFF =
  "border-zinc-300 bg-white text-zinc-700 hover:bg-zinc-100 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-300 dark:hover:bg-zinc-800";
const CHIP_ON = "border-brand-700 bg-brand-700 dark:border-brand-600 dark:bg-brand-600 text-white";

const byCategory = (a: Book, b: Book) =>
  Number(a.category === "other") - Number(b.category === "other") ||
  categoryLabel(a.category).localeCompare(categoryLabel(b.category));

/** Options for a book picker; `grouped` sorts them under category headings. */
export const bookOptions = (books: Book[], grouped = false): Option[] =>
  (grouped ? [...books].sort(byCategory) : books).map((book) => ({
    value: book.id,
    label: book.title + (book.flavour ? ` (${book.flavour})` : ""),
    hint: book.id,
    group: grouped ? categoryLabel(book.category) : undefined,
  }));

/**
 * Category chips plus a searchable dropdown for the books that have no real category.
 *
 * Kiwix files most one-off books under "other", which says nothing about them, so
 * those are offered by name instead of as one opaque chip.
 */
export function ScopeBar({
  books,
  category,
  book,
  onChange,
  center = false,
  up = false,
}: {
  books: Book[];
  category: string;
  book: string;
  onChange: (scope: { category: string; book: string }) => void;
  center?: boolean;
  up?: boolean;
}) {
  const categories = useMemo(() => categoryCounts(books).filter((c) => c.value !== "other"), [books]);
  const others = useMemo(() => books.filter((b) => b.category === "other"), [books]);
  const otherOptions = useMemo<Option[]>(
    () => [{ value: "*", label: "All other books", hint: String(others.length) }, ...bookOptions(others)],
    [others],
  );
  const otherValue = others.some((b) => b.id === book) ? book : category === "other" ? "*" : "";
  if (categories.length + (others.length ? 1 : 0) < 2) return null;

  return (
    <div className={`flex flex-wrap gap-1.5 ${center ? "justify-center" : ""}`} role="group" aria-label="Category">
      <button
        type="button"
        aria-pressed={!category && !book}
        onClick={() => onChange({ category: "", book: "" })}
        className={`${CHIP} ${!category && !book ? CHIP_ON : CHIP_OFF}`}
      >
        All
      </button>
      {categories.map((item) => (
        <button
          key={item.value}
          type="button"
          aria-pressed={category === item.value}
          onClick={() => onChange({ category: item.value, book: "" })}
          className={`${CHIP} ${category === item.value ? CHIP_ON : CHIP_OFF}`}
        >
          {categoryLabel(item.value)}
          <span className="opacity-60">{item.count}</span>
        </button>
      ))}
      {others.length > 0 && (
        <Combobox
          value={otherValue}
          options={otherOptions}
          placeholder="More"
          searchPlaceholder={`Search ${others.length} other books…`}
          align="right"
          up={up}
          className={`${CHIP} ${otherValue ? CHIP_ON : CHIP_OFF}`}
          onChange={(value) => onChange(value === "*" ? { category: "other", book: "" } : { category: "other", book: value })}
        />
      )}
    </div>
  );
}
