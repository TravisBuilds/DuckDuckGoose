import test from 'node:test';
import assert from 'node:assert/strict';

import {
  REDO_NOTE_MAX,
  REDO_PREFIX,
  REDO_PROMPT_MAX,
  redoEstimateRequest,
  redoNote,
  redoNoteText,
  redoPriceText,
  redoPromptAppend,
  redoRequest,
  redoStartedText,
} from './redo.mjs';

const codePoints = (text) => Array.from(text).length;

test('the note goes with the redo as one framed instruction', () => {
  assert.equal(redoPromptAppend('the framing is too loose'), 'Change requested: the framing is too loose.');
  assert.equal(redoPromptAppend('  make the cup\n red!  '), 'Change requested: make the cup red!');
  assert.equal(redoPromptAppend('Two cups, not one?'), 'Change requested: Two cups, not one?');
  assert.equal(REDO_PREFIX, 'Change requested: ');
  for (const empty of ['', '   \n ', null, undefined]) assert.equal(redoPromptAppend(empty), null);
});

test('a long note is cut to fit the redo prompt limit, never past it', () => {
  assert.equal(REDO_PROMPT_MAX, 1000);
  assert.equal(REDO_PREFIX.length + REDO_NOTE_MAX + 1, REDO_PROMPT_MAX);
  const fits = 'a'.repeat(REDO_NOTE_MAX);
  assert.equal(redoPromptAppend(fits), `${REDO_PREFIX}${fits}.`);
  assert.equal(codePoints(redoPromptAppend(fits)), REDO_PROMPT_MAX);

  const long = redoPromptAppend('word '.repeat(400));
  assert.ok(codePoints(long) <= REDO_PROMPT_MAX, `${codePoints(long)} characters`);
  assert.ok(long.startsWith(`${REDO_PREFIX}word word`) && long.endsWith('…'), long.slice(-20));

  // characters outside the basic plane count once (the API counts characters) and are never split
  const wide = redoPromptAppend('🦆'.repeat(2000));
  assert.ok(codePoints(wide) <= REDO_PROMPT_MAX);
  assert.ok(!/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/.test(wide), 'no lone surrogate');
  assert.ok(wide.endsWith('🦆…'));
});

test('the box holds the reason the picture was turned down until the owner types their own', () => {
  assert.equal(redoNote(undefined, 'the framing is too loose'), 'the framing is too loose');
  assert.equal(redoNote(null, 'the framing is too loose'), 'the framing is too loose');
  assert.equal(redoNote('closer on the cup', 'the framing is too loose'), 'closer on the cup');
  assert.equal(redoNote('', 'the framing is too loose'), '', 'a cleared box sends no note');
  assert.equal(redoNote(undefined, undefined), '');
});

test('the estimate and the redo send the same note, and the redo the price that was shown', () => {
  const estimate = { stills: 1, dry_run: false, total_usd_micros: 70_000 };
  assert.deepEqual(redoEstimateRequest('the framing is too loose'),
    { prompt_append: 'Change requested: the framing is too loose.' });
  assert.deepEqual(redoRequest('the framing is too loose', estimate),
    { prompt_append: 'Change requested: the framing is too loose.', approved_usd_micros: 70_000 });
  assert.deepEqual(redoEstimateRequest('  '), {});
  assert.deepEqual(redoRequest('', { ...estimate, total_usd_micros: 0 }), { approved_usd_micros: 0 });
  // a redo never overrides a missing estimate: it only ever sends the note and the approved price
  for (const body of [redoRequest('x', estimate), redoRequest('', estimate)]) {
    assert.deepEqual(Object.keys(body).filter((k) => k !== 'prompt_append'), ['approved_usd_micros']);
  }
});

test('the price is shown before anything is spent', () => {
  assert.equal(redoPriceText({ stills: 1, dry_run: false, total_usd_micros: 70_000 }),
    "1 new picture, about $0.07, charged to this video's budget");
  assert.equal(redoPriceText({ stills: 2, dry_run: false, total_usd_micros: 1_250_000 }),
    "2 new pictures, about $1.25, charged to this video's budget");
  assert.equal(redoPriceText({ stills: 1, dry_run: true, total_usd_micros: 60_000 }),
    '1 new picture, free: preview mode makes simple stand-in pictures');
  assert.equal(redoPriceText({ stills: 0, dry_run: false, total_usd_micros: 0 }),
    'Free: the start picture you uploaded is used again');
});

test('the confirmation says what goes with the redo, and the notice says it went', () => {
  assert.equal(redoNoteText(' the framing\nis too loose '), 'Your note goes with it: “the framing is too loose”');
  assert.equal(redoNoteText(''), 'No note: the picture is made again from the same description.');
  assert.equal(redoStartedText('S02', 'the framing is too loose'), 'Making picture S02 again with your note.');
  assert.equal(redoStartedText('S02', '  '), 'Making picture S02 again.');
});
