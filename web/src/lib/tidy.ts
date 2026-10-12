/**
 * Repairs for the Markdown that smaller language models get wrong.
 *
 * Models are asked to follow formatting rules, but small ones still leak reasoning,
 * forget to close code blocks, glue "##" to the heading text, invent citation styles and
 * so on. This cleans an answer up before it is rendered, copied or read aloud. Text inside
 * code blocks is never touched.
 */

const FENCE = /^\s{0,3}(`{3,}|~{3,})/;

/** Split into alternating prose and fenced-code blocks, closing a fence left open. */
function segments(text: string): { code: boolean; text: string }[] {
  const out: { code: boolean; text: string }[] = [];
  let buffer: string[] = [];
  let fence: string | null = null;
  const flush = (code: boolean) => {
    if (buffer.length) out.push({ code, text: buffer.join("\n") });
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
  if (fence !== null) {
    buffer.push(fence); // the model stopped, or is still writing, inside a code block
    flush(true);
  } else {
    flush(false);
  }
  return out;
}

/** Things models write instead of the "[1]" they were asked for. */
function citations(text: string): string {
  return text
    .replace(/[【［〔]\s*(\d+)\s*[】］〕]/g, "[$1]")
    .replace(/<sup>\s*\[?(\d+)\]?\s*<\/sup>/gi, "[$1]")
    .replace(/\[\^(\d+)\]/g, "[$1]")
    .replace(/\[\[(\d+)\]\]/g, "[$1]")
    .replace(/[[(]\s*(?:sources?|refs?|references?|citations?)\s*[:#]?\s*(\d+(?:\s*(?:,|and|&)\s*\d+)*)\s*[\])]/gi, "[$1]")
    .replace(/\[(\d+(?:\s*(?:,|and|&)\s*\d+)+)\]/g, (_, list: string) =>
      list
        .split(/\s*(?:,|and|&)\s*/)
        .map((n) => `[${n}]`)
        .join(""),
    )
    .replace(/\[(\d+)\s*[-–]\s*(\d+)\]/g, (whole, a: string, b: string) => {
      const [from, to] = [+a, +b];
      if (to <= from || to - from > 6) return whole;
      return Array.from({ length: to - from + 1 }, (_, i) => `[${from + i}]`).join("");
    });
}

/** Trim the spaces models leave inside bold markers, and settle an unmatched marker. */
function bold(line: string, streamingTail: boolean): string {
  const parts = line.split("**");
  if (parts.length < 2) return line;
  if (parts.length % 2 === 0) {
    // An odd number of markers: one has no partner.
    if (streamingTail) parts.push(""); // still being written: show it as bold so far
    else return parts.slice(0, -1).join("**") + parts[parts.length - 1];
  }
  for (let i = 1; i < parts.length; i += 2) parts[i] = parts[i].trim() || parts[i];
  return parts.join("**");
}

const isBullet = (line: string) => /^\s*([-*+]|\d+[.)])\s+\S/.test(line);
const isTableRow = (line: string) => /^\s*\|.*\|\s*$/.test(line);
const isSeparator = (line: string) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);

function prose(text: string, streamingTail: boolean): string {
  text = text
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/?(?:p|div|span|font|center)\b[^>]*>/gi, "")
    .replace(/<(?:b|strong)>([\s\S]*?)<\/(?:b|strong)>/gi, "**$1**")
    .replace(/<(?:i|em)>([\s\S]*?)<\/(?:i|em)>/gi, "*$1*")
    .replace(/<code>([\s\S]*?)<\/code>/gi, "`$1`")
    .replace(/<\/?(?:sub|u)>/gi, "");
  text = citations(text).replace(/<\/?sup>/gi, "");

  const lines = text.split("\n");
  const out: string[] = [];
  for (let index = 0; index < lines.length; index++) {
    let line = lines[index]
      .replace(/^(\s{0,3}#{1,6})(?=[^#\s])/, "$1 ") // "##Title"
      .replace(/^(\s{0,3})#\s+/, "$1## ") // a page-title heading is too loud for an answer
      .replace(/^(\s*)[•·●▪◦‣–—]\s+/, "$1- ")
      .replace(/^(\s*)\*(?=[^\s*])(?!.*\*)/, "$1* ") // "*item" with no closing asterisk
      .replace(/^(\s*\d+)\)\s+/, "$1. ")
      .replace(/^(\s*\d+)\.(?=[A-Za-z*`[])/, "$1. ");
    line = bold(line, streamingTail && index === lines.length - 1);
    if (streamingTail && index === lines.length - 1 && (line.match(/`/g)?.length ?? 0) % 2 === 1) line += "`";

    const previous = out[out.length - 1] ?? "";
    const startsBlock =
      /^\s{0,3}#{1,6}\s/.test(line) ||
      (isBullet(line) && !isBullet(previous) && !/^\s+\S/.test(line)) ||
      (isTableRow(line) && !isTableRow(previous));
    if (startsBlock && previous.trim() !== "") out.push("");
    out.push(line);

    // A table needs a |---| row under its header to be a table at all.
    const next = lines[index + 1];
    if (isTableRow(line) && !isTableRow(previous) && !isSeparator(line) && next !== undefined && isTableRow(next) && !isSeparator(next)) {
      const columns = line.trim().replace(/^\||\|$/g, "").split("|").length;
      out.push("|" + " --- |".repeat(columns));
    }
  }
  return out.join("\n");
}

export function tidyMarkdown(raw: string, options: { streaming?: boolean; hasSources?: boolean } = {}): string {
  const { streaming = false, hasSources = false } = options;
  let text = raw
    .replace(/\r\n?/g, "\n")
    // The server separates thinking from the answer; this is the fallback for anything it misses.
    .replace(/<(think|thinking|thought|thoughts|reasoning|reflection)>[\s\S]*?<\/\1>/gi, "")
    .replace(/^\s*<(think|thinking|thought|thoughts|reasoning|reflection)>[\s\S]*$/i, "") // still being written
    .replace(/<\|[a-z_]+\|>|<\/?s>|\[\/?INST\]/gi, "");
  // An answer that arrived as one line with literal "\n" in it.
  if (!text.includes("\n") && (text.match(/\\n/g)?.length ?? 0) >= 2) text = text.replace(/\\n/g, "\n").replace(/\\t/g, "  ");
  text = text.trim();
  // The whole answer wrapped in a ```markdown block.
  const wrapped = /^```(?:markdown|md)\s*\n([\s\S]*?)(?:\n```)?\s*$/i.exec(text);
  if (wrapped && !wrapped[1].includes("\n```\n")) text = wrapped[1];
  text = text.replace(/^(?:\*\*)?(?:answer|response|assistant)(?:\*\*)?\s*:\s*(?:\*\*)?\s*/i, "");

  const parts = segments(text);
  text = parts
    .map((part, index) => (part.code ? part.text : prose(part.text, streaming && index === parts.length - 1)))
    .join("\n")
    .replace(/\n{3,}/g, "\n\n");

  if (hasSources && !streaming) {
    // The sources are shown beside the answer; a list of them at the end is noise.
    text = text.replace(
      /\n+(?:#{1,6}\s*|\*\*)?(?:sources|references|citations)\s*:?\s*(?:\*\*)?\s*:?\s*\n+(?:\s*(?:[-*]|\d+[.)]|\[\d+\])[^\n]*\n?)+\s*$/i,
      "",
    );
  }
  return text.trim();
}
