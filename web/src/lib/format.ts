export function bytes(value: number): string {
  // Anything under one byte (a stalled speed is a small fraction) would give a negative exponent.
  if (!Number.isFinite(value) || value < 1) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const exponent = Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024)));
  const scaled = value / 1024 ** exponent;
  return `${scaled >= 100 || exponent === 0 ? scaled.toFixed(0) : scaled.toFixed(1)} ${units[exponent]}`;
}

export function duration(seconds: number | null): string {
  if (seconds == null || !isFinite(seconds)) return "";
  if (seconds < 60) return `${Math.ceil(seconds)} s`;
  if (seconds < 3600) return `${Math.ceil(seconds / 60)} min`;
  const hours = Math.floor(seconds / 3600);
  return `${hours} h ${Math.round((seconds % 3600) / 60)} min`;
}

export function count(value: number): string {
  return value.toLocaleString();
}

export function ago(timestamp: number | null): string {
  if (!timestamp) return "never";
  const seconds = Date.now() / 1000 - timestamp;
  if (seconds < 90) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400 * 2) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

export function day(timestamp: number): string {
  return new Date(timestamp * 1000).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

const LANGUAGES = new Intl.DisplayNames(undefined, { type: "language" });

function languageName(code: string): string {
  try {
    return LANGUAGES.of(code) ?? code;
  } catch {
    return code;
  }
}

/** Names for a comma-separated list of ISO codes, shortened to "English +11" when long. */
export function language(codes: string, limit = 2): string {
  const parts = codes.split(",").filter(Boolean);
  const names = parts.slice(0, limit).map(languageName).join(", ");
  return parts.length > limit ? `${names} +${parts.length - limit}` : names;
}

const CATEGORY_LABELS: Record<string, string> = {
  devdocs: "DevDocs",
  iFixit: "iFixit",
  stack_exchange: "Stack Exchange",
  ted: "TED",
  phet: "PhET",
  mooc: "MOOC",
  gutenberg: "Project Gutenberg",
  other: "Other",
};

export function categoryLabel(category: string): string {
  return CATEGORY_LABELS[category] ?? category.charAt(0).toUpperCase() + category.slice(1).replace(/_/g, " ");
}

/** Categories present in a list of books, most books first, with "other" last. */
export function categoryCounts(books: { category: string }[]): { value: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const book of books) counts.set(book.category, (counts.get(book.category) ?? 0) + 1);
  return [...counts]
    .map(([value, count]) => ({ value, count }))
    .sort((a, b) => Number(a.value === "other") - Number(b.value === "other") || b.count - a.count);
}
