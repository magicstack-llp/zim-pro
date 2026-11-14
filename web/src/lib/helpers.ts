/** Pure helpers shared by pages and unit-tested directly. */

/** Return only keys whose values differ from `previous`. */
export function changedKeys<T extends object>(current: T, previous: T): Partial<T> {
  const patch: Partial<T> = {};
  for (const key of Object.keys(current) as (keyof T)[]) {
    if (!Object.is(current[key], previous[key])) patch[key] = current[key];
  }
  return patch;
}

/** Parse a URL `start` query parameter into a non-negative integer offset. */
export function parseStart(raw: string | null): number {
  if (!raw) return 0;
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed < 0) return 0;
  return Math.floor(parsed);
}

const FENCE = /^\s{0,3}(`{3,}|~{3,})/;

function linkProseCitations(prose: string, valid: Set<number>): string {
  // Split on inline backtick code spans (`...`, ``...``, etc.) so code indices stay untouched.
  const parts = prose.split(/(`+[^`]*`+)/g);
  return parts
    .map((part, index) =>
      index % 2 === 1
        ? part
        : part.replace(/\[(\d+)\](?!\()/g, (whole, n) => (valid.has(+n) ? `[${n}](#cite-${n})` : whole)),
    )
    .join("");
}

/** Turn "[2]" in Markdown prose into a citation link, leaving fenced and inline code untouched. */
export function linkCitations(text: string, sourceNumbers: number[]): string {
  if (!sourceNumbers.length) return text;
  const valid = new Set(sourceNumbers);
  const out: string[] = [];
  let buffer: string[] = [];
  let fence: string | null = null;
  const flush = (code: boolean) => {
    if (!buffer.length) return;
    const chunk = buffer.join("\n");
    out.push(code ? chunk : linkProseCitations(chunk, valid));
    buffer = [];
  };
  for (const line of text.split("\n")) {
    const mark = FENCE.exec(line)?.[1];
    if (fence === null && mark) {
      flush(false);
      fence = mark[0].repeat(mark.length);
      buffer.push(line);
    } else if (fence !== null && mark && mark[0] === fence[0] && mark.length >= fence.length && line.trim() === mark) {
      buffer.push(line);
      fence = null;
      flush(true);
    } else {
      buffer.push(line);
    }
  }
  flush(fence !== null);
  return out.join("\n");
}
