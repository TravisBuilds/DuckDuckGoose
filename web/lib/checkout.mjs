/**
 * Easy checkout (docs/EASY-CHECKOUT.md): one price for each paid thing, in plain words, next to one
 * button. The words of the price card that character sheets, first frames, the mood board, a picture
 * redo, the shot plan and the start of a video share, and the calm words for a refused spend.
 *
 * A price only ever comes from the API's estimate, and the button sends that same estimate as the
 * approved total: the server prices the thing again when it makes it and refuses a higher price. With
 * no estimate there is no price and nothing to approve.
 */
import { formatUsdMicros } from './balance.mjs';

/** "$0.09", "$1,234.50", "$9": the cents are left off only when there are none. */
export function priceText(micros) {
  const text = formatUsdMicros(micros);
  return text.endsWith('.00') ? text.slice(0, -3) : text;
}

function usable(micros) {
  return typeof micros === 'number' && Number.isFinite(micros) && micros >= 0;
}

/**
 * The price of one paid thing from its estimate ({ dry_run, total_usd_micros }), or null without one:
 * { free, words, approve }. words is what the card shows ("about $0.09", or "free" in a test run);
 * approve is the approved total the button sends, always the estimate's own total.
 */
export function quote(estimate) {
  if (!estimate || !usable(estimate.total_usd_micros)) return null;
  const free = estimate.dry_run === true;
  return { free, words: free ? 'free' : `about ${priceText(estimate.total_usd_micros)}`, approve: estimate.total_usd_micros };
}

/** The one line in big type: "3 options: about $0.09", "Mood board: free". */
export function headline(what, price) {
  return price ? `${what}: ${price.words}` : what;
}

/**
 * A make-it button's request: its body plus the approved total, which is always the shown price's own
 * total (field approved_usd_micros for a picture redo). With no price there is nothing to approve, so
 * it throws and nothing is sent.
 */
export function approvedRequest(body, estimate, field = 'approved_total_usd_micros') {
  const price = quote(estimate);
  if (!price) throw new Error('There is no price to approve yet.');
  return { ...body, [field]: price.approve };
}

export const PROMISE = 'If the price goes up before we make it, we ask you first.';
export const FREE_NOTE = 'A test run, so nothing is charged.';

/**
 * The calm line under the button: a test run's, or the live promise. The promise is only made where
 * the server keeps it (it checks the approved total again when it makes the thing and refuses a higher
 * price), so a caller without that check passes kept: false and gets no promise.
 */
export function promise(price, { kept = true } = {}) {
  if (!price) return null;
  if (price.free) return FREE_NOTE;
  return kept ? PROMISE : null;
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
}

/**
 * The price of a whole video from the offline estimate of its shot list (the wizard's draft or the
 * episode's preflight: total_usd_micros, per_shot, unpriced): the list price of one try of each picture
 * and clip. { free, words, approve, rows } or null without an estimate. In a test run it is free and
 * the breakdown says what a live video would cost. A shot whose clip has no price makes the words "at
 * least", never a smaller "about".
 */
export function videoQuote(estimate, { free = false } = {}) {
  if (!estimate || !usable(estimate.total_usd_micros)) return null;
  const total = estimate.total_usd_micros;
  const shots = Object.values(estimate.per_shot ?? {});
  const stills = shots.reduce((sum, s) => sum + (usable(s?.stills_usd_micros) ? s.stills_usd_micros : 0), 0);
  const clips = shots.reduce((sum, s) => sum + (usable(s?.clip_usd_micros) ? s.clip_usd_micros : 0), 0);
  const unpriced = (estimate.unpriced ?? []).length;
  const live = `${unpriced > 0 ? 'at least' : 'about'} ${priceText(total)}`;
  if (free) {
    return { free: true, words: 'free', approve: 0,
      rows: [{ label: 'The same video, live', value: live }] };
  }
  const rows = [{ label: 'Scene pictures', value: priceText(stills) }, { label: 'Clips', value: priceText(clips) }];
  if (unpriced > 0) rows.push({ label: 'Not priced yet', value: plural(unpriced, 'clip') });
  return { free: false, words: live, approve: total, rows };
}

/** All the clips of a video, at list price, from the same estimate ("about $5.34"), or null without one. */
export function clipsPrice(estimate) {
  if (!estimate || !usable(estimate.total_usd_micros)) return null;
  const clips = Object.values(estimate.per_shot ?? {})
    .reduce((sum, s) => sum + (usable(s?.clip_usd_micros) ? s.clip_usd_micros : 0), 0);
  return `${(estimate.unpriced ?? []).length > 0 ? 'at least' : 'about'} ${priceText(clips)}`;
}

/** One shot's clip, at list price, from the same estimate ("about $1.20"), or null when it has none. */
export function clipPrice(estimate, shotId) {
  const micros = estimate?.per_shot?.[shotId]?.clip_usd_micros;
  return usable(micros) ? `about ${priceText(micros)}` : null;
}

const AMOUNT = String.raw`\$[\d,]+(?:\.\d+)?`;

function tidy(amount) {
  return amount.replace(/(\.\d\d)0+$/, '$1');
}

const NO_PRICE = /override_unavailable_estimates|estimates? (?:are |is )?unavailable|estimate requests? failed|could not (?:get|check) (?:a |the |today's )?prices?/i;
const BALANCE_CHECK = /no manual \w+ balance recorded|balance was recorded [\d.]+ ?h ago|unreadable balance timestamp/i;
const BALANCE_LOW = /balance check failed|balance insufficient|GC\.01 headroom/i;
const BUDGET = /would exceed (?:its |the )?episode cap|budget cap exceeded|would pass its \d+% stop|left before (?:its|their|the) stop/i;

/**
 * A refused spend in calm words with one next action, or null when the refusal is not about money
 * (those keep the server's own words): { kind, text, action }. When the price went up the action
 * fetches the new price; every other one says nothing was charged and who can help. Server words
 * (floors, headroom, GC.01, provider names, the estimates override) never reach the screen.
 */
export function moneyProblem(message) {
  const text = String(message ?? '');
  const rose = text.match(new RegExp(`(?:estimate is now|now costs) (${AMOUNT})`, 'i'));
  if (rose) {
    return { kind: 'price_rose', text: `The price went up to ${tidy(rose[1])} since you looked. Nothing was charged.`,
      action: 'See the new price' };
  }
  if (NO_PRICE.test(text)) {
    return { kind: 'no_price', text: "We couldn't get today's prices, so nothing was paid for. Try again in a few minutes.",
      action: 'Try again' };
  }
  if (BALANCE_CHECK.test(text)) {
    return { kind: 'balance_check',
      text: 'The studio needs a fresh balance check first. Nothing was charged. An admin can record one on the costs page.',
      action: 'Try again' };
  }
  if (BALANCE_LOW.test(text)) {
    return { kind: 'balance', text: "The studio's balance is too low for this right now. Nothing was charged. An admin can top it up.",
      action: 'Try again' };
  }
  if (BUDGET.test(text)) {
    const left = text.match(new RegExp(`has (${AMOUNT}) left before`, 'i'));
    return { kind: 'budget',
      text: `This would go past its budget${left ? ` (${tidy(left[1])} left)` : ''}. Nothing was charged. An admin can raise it.`,
      action: 'Try again' };
  }
  return null;
}

/** What a refusal shows: a money one in calm words, any other as the server said it. */
export function calmText(message) {
  return moneyProblem(message)?.text ?? String(message ?? '');
}

/**
 * Ledger words: they belong behind "See the breakdown" or on the admin pages, never on the face of a
 * money screen. The copy tests check the words each screen shows up front.
 */
export const LEDGER_WORDS = [
  'budget line', 'reserve', 'reserved', 'floor', 'headroom', 'credits', 'providers', 'provider', 'GC.01', 'override',
  'ledger', 'estimate', 'approved total', 'charged to',
];

/** The ledger words in a text, case-insensitive and whole words only. */
export function ledgerWords(text) {
  const found = [];
  for (const word of LEDGER_WORDS) {
    const escaped = word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    if (new RegExp(`\\b${escaped}\\b`, 'i').test(String(text ?? ''))) found.push(word);
  }
  return found;
}
