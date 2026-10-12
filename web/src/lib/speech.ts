import { authHeaders } from "./api";

/** Markdown answer text reduced to what should be spoken. */
export function speakable(markdown: string): string {
  return markdown
    .replace(/```[\s\S]*?```/g, " Code example omitted. ")
    .replace(/\[(\d+)\](?!\()/g, "")
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^\s*\|?[\s:|-]+\|?\s*$/gm, "")
    .replace(/\|/g, ", ")
    .replace(/^\s{0,3}(#{1,6}|[-*+>]|\d+[.)])\s+/gm, "")
    .replace(/[*_`~]/g, "")
    .replace(/\n{2,}/g, ". ")
    .replace(/\s+/g, " ")
    .replace(/(\s*\.){2,}/g, ".")
    .trim();
}

/** Split into pieces a speech model handles quickly, breaking at sentence ends. */
export function chunks(text: string, size = 700): string[] {
  const parts: string[] = [];
  let current = "";
  for (const sentence of text.match(/[^.!?]+[.!?]*\s*/g) ?? [text]) {
    if (current && current.length + sentence.length > size) {
      parts.push(current.trim());
      current = "";
    }
    current += sentence;
  }
  if (current.trim()) parts.push(current.trim());
  return parts;
}

type Listener = (state: "idle" | "loading" | "playing") => void;

/** One answer is read at a time; starting another stops the first. */
let stopCurrent: (() => void) | null = null;

export function stopSpeaking(): void {
  stopCurrent?.();
  stopCurrent = null;
}

export async function speak(markdown: string, mode: string, onState: Listener): Promise<void> {
  stopSpeaking();
  const text = speakable(markdown);
  if (!text) return;
  let cancelled = false;
  let audio: HTMLAudioElement | null = null;
  const controller = new AbortController();
  const finish = () => {
    if (stopCurrent === stop) stopCurrent = null;
    onState("idle");
  };
  const stop = () => {
    cancelled = true;
    controller.abort();
    audio?.pause();
    window.speechSynthesis?.cancel();
    onState("idle");
  };
  stopCurrent = stop;

  if (mode !== "provider") {
    if (!("speechSynthesis" in window)) throw new Error("This browser has no built-in voice.");
    onState("playing");
    for (const piece of chunks(text, 220)) {
      const utterance = new SpeechSynthesisUtterance(piece);
      await new Promise<void>((resolve) => {
        utterance.onend = utterance.onerror = () => resolve();
        window.speechSynthesis.speak(utterance);
      });
      if (cancelled) return;
    }
    return finish();
  }

  onState("loading");
  const fetchPiece = async (piece: string): Promise<string> => {
    const response = await fetch("/api/ai/speech", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ text: piece }),
      signal: controller.signal,
    });
    if (!response.ok) throw new Error((await response.json().catch(() => null))?.detail ?? `Speech failed (HTTP ${response.status})`);
    return URL.createObjectURL(await response.blob());
  };
  try {
    const pieces = chunks(text);
    // Fetch one piece ahead so playback does not pause between them.
    let next: Promise<string> | null = fetchPiece(pieces[0]);
    for (let index = 0; index < pieces.length && next; index++) {
      const url: string = await next;
      next = index + 1 < pieces.length ? fetchPiece(pieces[index + 1]) : null;
      next?.catch(() => {});
      if (cancelled) return;
      onState("playing");
      audio = new Audio(url);
      await new Promise<void>((resolve, reject) => {
        audio!.onended = () => resolve();
        audio!.onerror = () => reject(new Error("The browser could not play the audio."));
        audio!.play().catch(reject);
      }).finally(() => URL.revokeObjectURL(url));
      if (cancelled) return;
    }
    finish();
  } catch (reason) {
    if (cancelled) return;
    finish();
    throw reason;
  }
}
