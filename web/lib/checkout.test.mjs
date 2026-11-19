import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  FREE_NOTE, LEDGER_WORDS, PROMISE, approvedRequest, calmText, clipPrice, clipsPrice, headline, ledgerWords, moneyProblem, priceText,
  promise, quote, videoQuote,
} from './checkout.mjs';
import { ICON_CHARACTERS, bannedWords } from './copy-rules.mjs';
import { redoRequest } from './redo.mjs';

const live = { dry_run: false, total_usd_micros: 90_000 };
const dry = { dry_run: true, total_usd_micros: 180_000 };
const offline = {
  total_usd_micros: 7_440_000,
  per_shot: {
    S01: { stills_usd_micros: 60_000, clip_usd_micros: 1_200_000, pre_approved: false },
    S02: { stills_usd_micros: 180_000, clip_usd_micros: 6_000_000, pre_approved: false },
  },
  unpriced: [],
};

test('a price reads as dollars and cents, without ".00" on whole dollars', () => {
  assert.equal(priceText(90_000), '$0.09');
  assert.equal(priceText(9_000_000), '$9');
  assert.equal(priceText(1_234_500_000), '$1,234.50');
  assert.equal(priceText(0), '$0');
});

test('one price per thing: the estimate itself is the approved total, "about" when live, free in a test run', () => {
  assert.deepEqual(quote(live), { free: false, words: 'about $0.09', approve: 90_000 });
  assert.deepEqual(quote(dry), { free: true, words: 'free', approve: 180_000 });
  // no estimate, no price, nothing to approve
  for (const none of [null, undefined, {}, { dry_run: false }, { total_usd_micros: -1 }, { total_usd_micros: Number.NaN }]) {
    assert.equal(quote(none), null);
  }
  // only an explicit test run is free: an estimate that does not say is priced
  assert.deepEqual(quote({ total_usd_micros: 40_000 }), { free: false, words: 'about $0.04', approve: 40_000 });
  assert.equal(headline('3 options', quote(live)), '3 options: about $0.09');
  assert.equal(headline('Mood board', quote(dry)), 'Mood board: free');
  assert.equal(headline('Mood board', null), 'Mood board');
});

test('the calm line: a test run says nothing is charged; the live promise only where the server keeps it', () => {
  assert.equal(promise(quote(dry)), FREE_NOTE);
  assert.equal(promise(quote(live)), PROMISE);
  assert.equal(promise(quote(live), { kept: false }), null);
  assert.equal(promise(quote(dry), { kept: false }), FREE_NOTE);
  assert.equal(promise(null), null);
});

test('every make-it button sends the price it shows as its approved total, and nothing without a price', () => {
  // the mood board, a character sheet and a round of first frames send approved_total_usd_micros
  assert.equal(headline('Your mood board', quote(live)), 'Your mood board: about $0.09');
  assert.deepEqual(approvedRequest({ feedback: 'warmer light' }, live), { feedback: 'warmer light', approved_total_usd_micros: 90_000 });
  assert.deepEqual(approvedRequest({ stage: 'options', source: 'text', count: 3 }, live),
    { stage: 'options', source: 'text', count: 3, approved_total_usd_micros: 90_000 });
  assert.deepEqual(approvedRequest({ change: 'closer to the boat', keep_changes: true }, dry),
    { change: 'closer to the boat', keep_changes: true, approved_total_usd_micros: 180_000 });
  // a picture redo sends approved_usd_micros, with its note
  assert.deepEqual(redoRequest('the boat closer', { dry_run: false, stills: 1, total_usd_micros: 40_000 }),
    { prompt_append: 'Change requested: the boat closer.', approved_usd_micros: 40_000 });
  assert.deepEqual(redoRequest('', { dry_run: true, stills: 1, total_usd_micros: 60_000 }), { approved_usd_micros: 60_000 });
  // no price on screen, nothing sent
  for (const none of [null, undefined, {}, { total_usd_micros: Number.NaN }]) {
    assert.throws(() => approvedRequest({ feedback: '' }, none), /no price to approve/);
    assert.throws(() => redoRequest('', none), /no price to approve/);
  }
});

test('a whole video: the list price of one try of each picture and clip, by kind in the breakdown', () => {
  assert.deepEqual(videoQuote(offline), {
    free: false, words: 'about $7.44', approve: 7_440_000,
    rows: [{ label: 'Scene pictures', value: '$0.24' }, { label: 'Clips', value: '$7.20' }],
  });
  // a test run is free; its breakdown says what the same video would cost live
  assert.deepEqual(videoQuote(offline, { free: true }), {
    free: true, words: 'free', approve: 0, rows: [{ label: 'The same video, live', value: 'about $7.44' }],
  });
  // a clip without a price never makes the total read as the whole price
  const partial = { ...offline, unpriced: ['S03: no rate for this clip'] };
  assert.equal(videoQuote(partial).words, 'at least $7.44');
  assert.deepEqual(videoQuote(partial).rows.at(-1), { label: 'Not priced yet', value: '1 clip' });
  assert.equal(videoQuote(null), null);
  assert.equal(videoQuote({ per_shot: {} }), null);
  assert.equal(clipPrice(offline, 'S02'), 'about $6');
  assert.equal(clipPrice(offline, 'S09'), null);
  assert.equal(clipPrice({ per_shot: { S01: { clip_usd_micros: null } } }, 'S01'), null);
  assert.equal(clipPrice(null, 'S01'), null);
  // all the clips, for the line under the scene pictures' approval
  assert.equal(clipsPrice(offline), 'about $7.20');
  assert.equal(clipsPrice(partial), 'at least $7.20');
  assert.equal(clipsPrice(null), null);
});

test('the price going up asks again, with the new price, and says nothing was charged', () => {
  for (const said of [
    'The estimate is now $0.12 (you approved $0.09); review it and approve again',
    'The redo now costs $0.05 (you approved $0.04); review the new price and approve again',
    'The estimate is now $0.1800, just over what you approved ($0.1800); review it and approve again',
  ]) {
    const problem = moneyProblem(said);
    assert.equal(problem.kind, 'price_rose', said);
    assert.equal(problem.action, 'See the new price');
    assert.match(problem.text, /^The price went up to \$0\.\d+ since you looked\. Nothing was charged\.$/);
  }
  assert.equal(moneyProblem('The estimate is now $0.12 (you approved $0.09); review it and approve again').text,
    'The price went up to $0.12 since you looked. Nothing was charged.');
  assert.equal(moneyProblem('The estimate is now $0.1800, just over what you approved ($0.1800)').text,
    'The price went up to $0.18 since you looked. Nothing was charged.');
});

test('every other money refusal: calm words, nothing charged, one next action, no server words', () => {
  const cases = [
    ['Balance check failed: higgsfield balance insufficient: $0.04 < required $0.10 (estimate $0.09 + 10% reserve)', 'balance'],
    ['Balance check failed: Higgsfield balance $0.04 is below the $5.00 floor plus this estimate ($0.09)', 'balance'],
    ['GC.01 headroom: balance 10.00 USD minus holds/spend would leave 4.00 USD, below the 5.00 USD headroom', 'balance'],
    ['Nothing was charged: GC.01: no manual Higgsfield balance recorded; record it first (`hfvg budget set-balance <usd>` or POST /api/budget/balance)', 'balance_check'],
    ['Nothing was charged: GC.01: the manual Higgsfield balance was recorded 30.2 h ago (at 2026-11-18T08:00:00+00:00), older than 24 h (HFVG_BALANCE_MAX_AGE_HOURS); check the balance and record it again before any paid job', 'balance_check'],
    ["This episode's budget for draft pictures has $0.40 left before its stop, and these first frames cost about $0.09. Nothing was charged; ask an admin about the budget.", 'budget'],
    ["This Universe's mood board budget has $3.20 left before its stop, and this board costs about $4.00. Nothing was charged; ask an admin about the budget.", 'budget'],
    ['Episode ep01abc would exceed episode cap. Current: 59.00 USD, requested: 2.00 USD, cap: 60.00 USD', 'budget'],
    ['G1 approval refused: Cannot approve G1 in live mode when estimates are unavailable. Provider estimate requests failed. Pass override_unavailable_estimates=True to proceed anyway.', 'no_price'],
  ];
  for (const [said, kind] of cases) {
    const problem = moneyProblem(said);
    assert.ok(problem, said);
    assert.equal(problem.kind, kind, said);
    assert.equal(problem.action, 'Try again');
    assert.match(problem.text, /nothing was (?:charged|paid for)/i);
    assert.deepEqual(ledgerWords(problem.text), [], problem.text);
    assert.doesNotMatch(problem.text, /higgsfield|GC\.01|override|floor|headroom|USD|\bcap\b/i, problem.text);
    assert.equal(calmText(said), problem.text);
  }
  assert.equal(moneyProblem(cases[5][0]).text, 'This would go past its budget ($0.40 left). Nothing was charged. An admin can raise it.');
  assert.equal(moneyProblem(cases[7][0]).text, 'This would go past its budget. Nothing was charged. An admin can raise it.');
});

test('the estimates override is never shown, whatever the refusal around it', () => {
  for (const said of [
    'Pass override_unavailable_estimates=True to proceed anyway.',
    'G1 approval refused: Cannot approve G1 in live mode when estimates are unavailable.',
  ]) {
    assert.doesNotMatch(calmText(said), /override|estimates_|=True/i);
  }
});

test('a refusal that is not about money keeps the server\'s own words', () => {
  for (const said of [
    'Lock the look of Pip before this video starts',
    "Mood boards are made by the studio's worker, and the worker running now cannot make them yet. Nothing was charged. Ask an admin to update the worker, then try again.",
    'Sheet images for this version are being made; wait for them',
  ]) {
    assert.equal(moneyProblem(said), null, said);
    assert.equal(calmText(said), said);
  }
  assert.equal(calmText(null), '');
});

test('ledger words are found whole and in any case, and stay off the checkout words', () => {
  assert.deepEqual(ledgerWords('Left in the draft pictures budget line'), ['budget line']);
  assert.deepEqual(ledgerWords('Uses real CREDITS'), ['credits']);
  assert.deepEqual(ledgerWords('This would go past its budget'), []);
  assert.deepEqual(ledgerWords('preserves'), []);
  assert.ok(LEDGER_WORDS.includes('GC.01') && LEDGER_WORDS.includes('override'));
  const words = [PROMISE, FREE_NOTE, headline('This video', videoQuote(offline)), headline('3 options', quote(live)),
    ...videoQuote(offline).rows.map((r) => r.label), ...videoQuote(offline, { free: true }).rows.map((r) => r.label)];
  for (const text of words) {
    assert.deepEqual(ledgerWords(text), [], text);
    assert.deepEqual(bannedWords(text), [], text);
    assert.doesNotMatch(text, ICON_CHARACTERS, text);
  }
});
