/**
 * The scene-picture redo at G2 ("Make this picture again"): the owner's note goes with the redo as its
 * prompt_append, and the redo's price is shown before anything is spent (POST .../retry/estimate, then
 * POST .../retry with the approved amount, which the API checks against a fresh estimate and the
 * balance gate). Pure functions, shared by the episode page and its tests.
 */

import { headline, promise, quote } from './checkout.mjs';

/** RetryShotRequest.prompt_append's limit, in characters. */
export const REDO_PROMPT_MAX = 1000;
/** The note is appended to the picture's description, so it is framed as an instruction. */
export const REDO_PREFIX = 'Change requested: ';
/** The longest note that still fits the limit with its framing (the note box takes no more). */
export const REDO_NOTE_MAX = REDO_PROMPT_MAX - REDO_PREFIX.length - 1;

function words(text) {
  return String(text ?? '').split(/\s+/).filter(Boolean).join(' ');
}

/** The note a redo uses: what the owner typed in the box, else the reason they turned the picture down with. */
export function redoNote(typed, recorded) {
  return typed === undefined || typed === null ? String(recorded ?? '') : String(typed);
}

/** The note as the redo's prompt_append: one framed sentence of at most REDO_PROMPT_MAX characters, or null. */
export function redoPromptAppend(note) {
  const text = words(note);
  if (!text) return null;
  const chars = Array.from(text);
  const fitted = chars.length > REDO_NOTE_MAX ? `${chars.slice(0, REDO_NOTE_MAX - 1).join('').trimEnd()}…` : text;
  return `${REDO_PREFIX}${/[.!?…]$/.test(fitted) ? fitted : `${fitted}.`}`;
}

/** The body of POST .../retry/estimate. */
export function redoEstimateRequest(note) {
  const prompt_append = redoPromptAppend(note);
  return prompt_append ? { prompt_append } : {};
}

/** The body of POST .../retry: the same note, for the price the owner was shown. */
export function redoRequest(note, estimate) {
  return { ...redoEstimateRequest(note), approved_usd_micros: estimate.total_usd_micros };
}

/** What the redo costs, in the checkout's one line, before the owner confirms it. */
export function redoPriceText(estimate) {
  const n = estimate.stills;
  if (n === 0) return 'This picture: free';
  return headline(`${n} new picture${n === 1 ? '' : 's'}`, quote(estimate));
}

/** The calm line under the redo's button. */
export function redoLine(estimate) {
  if (estimate.stills === 0) return 'Your uploaded start picture is used again, so nothing is charged.';
  return promise(quote(estimate));
}

/** What goes with the redo. */
export function redoNoteText(note) {
  const text = words(note);
  return text ? `Your note goes with it: “${text}”` : 'No note: the picture is made again from the same description.';
}

/** The notice once the redo is on its way. */
export function redoStartedText(shotId, note) {
  return redoPromptAppend(note) ? `Making picture ${shotId} again with your note.` : `Making picture ${shotId} again.`;
}
