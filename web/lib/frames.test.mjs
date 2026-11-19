import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ICON_CHARACTERS, bannedWords } from './copy-rules.mjs';
import {
  CHANGE_EXAMPLES, aboutLine, anyMaking, changeBlocker, changesLine, framesCount, framesSummary, inItLine, listWords, makeLabel,
  optionName, pickedOption, priceRows, priceStatus, priceTotal, referencesLine, roundNote, roundTitle, shotBlocker,
  shotStatus,
} from './frames.mjs';

const shot = (extra = {}) => ({
  shot_id: 'A01', description: 'Wren at Harbour cabin, with Brass key, dusk', beat: 'Wren climbs to the lamp room',
  time_of_day: 'dusk', characters: ['Wren'], place: 'Harbour cabin', props: ['Brass key'], references: 3, no_photos: [],
  aspect_ratio: '9:16', notes: [], problems: [], ready: true, changes: [], making: false, changed: false, rounds: [],
  pick: null, ...extra,
});
const round = (extra = {}) => ({
  set_id: 'cf_0123456789ab', shot_id: 'A01', round: 1, status: 'ready', changes: [], option_count: 3, dry_run: true,
  estimate_usd_micros: 0, error: null, requested_by: 'usr_1', created_at: '', finished_at: '', made: 3, current: true,
  options: [], ...extra,
});
const view = (extra = {}) => ({
  episode_id: 'ep1', options_per_round: 3, dry_run: true, words_hidden: false, uploaded: true, shots: [shot()],
  error: null, may_change: true, phase: 'open', locked_reason: null, can_change: true, ...extra,
});
const dry = { shot_id: 'A01', round: 1, count: 3, per_image_usd_micros: [20000, 20000, 20000], total_usd_micros: 60000,
  dry_run: true, changes: [], characters: [], place: null, props: [], references: 0, notes: [], budget: null, summary: '' };
const live = { ...dry, dry_run: false, per_image_usd_micros: [30000, 30000, 30000], total_usd_micros: 90000,
  budget: { cap_usd_micros: 8000000, stop_usd_micros: 6400000, spent_usd_micros: 0, reserved_usd_micros: 0,
    available_usd_micros: 6400000 } };

test('the shot says who and what its start frame shows, in plain words', () => {
  assert.equal(inItLine(shot()), 'Wren at Harbour cabin, with Brass key');
  assert.equal(inItLine(shot({ characters: ['Wren', 'Pip', 'Moss'], props: [] })), 'Wren, Pip and Moss at Harbour cabin');
  assert.equal(inItLine(shot({ characters: [], place: null })), 'With Brass key');
  assert.equal(inItLine(shot({ characters: [], props: [] })), 'At Harbour cabin');
  assert.equal(inItLine(shot({ characters: [], place: null, props: [] })), 'No character, place or prop named in this shot');
  assert.equal(listWords(['A', 'B']), 'A and B');
  assert.equal(referencesLine(1), '1 reference photo guides it');
  assert.equal(referencesLine(3), '3 reference photos guide it');
  assert.match(referencesLine(0), /^No reference photos yet/);
});

test('the card describes the frame in plain words and names the missing photos once', () => {
  assert.equal(aboutLine(shot()), 'Wren at Harbour cabin, with Brass key, dusk');
  assert.equal(aboutLine(shot({ description: 'Mara at the harbour, dusk, close on her face' })),
    'Mara at the harbour, dusk, close on her face');
  // a studio that sends no description: who and what is in it
  assert.equal(aboutLine(shot({ description: '' })), 'Wren at Harbour cabin, with Brass key');
  assert.equal(aboutLine(shot({ description: undefined, characters: [], place: null, props: [] })),
    'No character, place or prop named in this shot');
  assert.equal(referencesLine(0, ['Lead']), 'No reference photos of Lead yet: the words guide it');
  assert.equal(referencesLine(0, ['Wren', 'Brass key']), 'No reference photos of Wren and Brass key yet: the words guide it');
  assert.equal(referencesLine(2, ['Pip']), '2 reference photos guide it; none of Pip yet');
  assert.equal(referencesLine(1, []), '1 reference photo guides it');
  assert.equal(referencesLine(0, null), 'No reference photos yet: the words guide it');
});

test('each shot reads where it stands, in the colour that means it', () => {
  assert.deepEqual(shotStatus(shot()), { tone: 'wait', text: 'No start frame yet (optional)' });
  assert.deepEqual(shotStatus(shot({ rounds: [round()] })), { tone: 'todo', text: 'Options ready: pick one' });
  assert.deepEqual(shotStatus(shot({ making: true, rounds: [round({ status: 'making', made: 0 })] })),
    { tone: 'run', text: 'Making options now' });
  assert.deepEqual(shotStatus(shot({ problems: ['Lock the look of Wren before first frames are made'] })),
    { tone: 'bad', text: 'Waiting on something first' });
  assert.deepEqual(shotStatus(shot({ changed: true, rounds: [round({ current: false })] })),
    { tone: 'todo', text: 'The shot changed: make new options' });
  const pick = { source: 'here', option_id: 'cframe-0123456789ab-1', image_url: '/x', thumb_url: '/x/thumb', stale: false,
    picked_by: 'usr_1', picked_at: 'now' };
  assert.deepEqual(shotStatus(shot({ pick })), { tone: 'ok', text: 'Start frame locked' });
  assert.equal(shotStatus(shot({ pick: { ...pick, stale: true } })).tone, 'todo');
  assert.deepEqual(shotStatus(shot({ pick: { ...pick, source: 'shot_list', option_id: null } })),
    { tone: 'ok', text: 'Start frame set in the shot list' });
  assert.equal(pickedOption(shot({ pick })), 'cframe-0123456789ab-1');
  assert.equal(pickedOption(shot({ pick: { ...pick, source: 'shot_list' } })), null);
});

test('the section counts the shots with a start frame', () => {
  const pick = { source: 'here', option_id: 'o', image_url: null, thumb_url: null, stale: false, picked_by: null, picked_at: null };
  const shots = [shot({ pick }), shot({ shot_id: 'A02' }), shot({ shot_id: 'A03', pick })];
  assert.deepEqual(framesCount({ shots }), { picked: 2, shots: 3 });
  assert.equal(framesSummary({ shots }), 'Start frames picked: 2 of 3 shots');
  assert.equal(framesSummary({ shots: [shot()] }), 'Start frames picked: 0 of 1 shot');
  assert.equal(framesSummary({ shots: [] }), 'No shots yet');
  assert.equal(anyMaking({ shots }), false);
  assert.equal(anyMaking({ shots: [...shots, shot({ making: true })] }), true);
});

test('the price comes first: free in a test run, the provider price when live', () => {
  assert.equal(priceTotal(dry), 'Free');
  assert.equal(priceStatus(dry), 'Test run');
  assert.deepEqual(priceRows(dry), [{ label: '3 options', value: 'Free stand-ins' }]);
  assert.equal(makeLabel(dry), 'Make 3 options');
  assert.equal(priceTotal(live), '$0.09');
  assert.equal(priceStatus(live), 'Checked just now');
  assert.deepEqual(priceRows(live), [{ label: '3 options', value: '$0.03 each' },
    { label: 'Left in the draft pictures budget', value: '$6.40' }]);
  assert.equal(makeLabel(live), 'Make 3 options for $0.09');
  assert.equal(makeLabel({ ...dry, count: 1 }), 'Make 1 option');
  assert.equal(makeLabel(null), 'Make options');
});

test('changes in plain words: what is kept, each round names the newest, the box says why it is off', () => {
  assert.equal(changesLine([]), null);
  assert.equal(changesLine(['make it night', 'closer on her face']), 'Changes so far: make it night; closer on her face');
  assert.equal(roundTitle(round()), 'First try');
  assert.equal(roundTitle(round({ round: 3, changes: ['make it night', 'closer on her face'] })), 'Try 3, with closer on her face');
  assert.equal(changeBlocker(''), 'Turns on once you say what to change.');
  assert.equal(changeBlocker('   '), 'Turns on once you say what to change.');
  assert.equal(changeBlocker('x'.repeat(201)), 'Keep it under 200 letters.');
  assert.equal(changeBlocker('make it night'), null);
  assert.ok(CHANGE_EXAMPLES.length >= 3 && CHANGE_EXAMPLES.includes('make it night'));
});

test('a round that stopped early says what was made and why', () => {
  assert.equal(roundNote(round()), null);
  assert.equal(roundNote(round({ status: 'making', made: 0 })), 'The studio is making them now');
  assert.equal(roundNote(round({ status: 'making', made: 1 })), '1 of 3 made so far');
  const reason = 'The price of a picture went up after you approved it, so the studio stopped before paying more. Check the price again.';
  assert.equal(roundNote(round({ status: 'partial', made: 1, error: reason })), `1 of 3 made. ${reason}`);
  assert.equal(roundNote(round({ status: 'failed', made: 0, error: null })), 'None were made. The studio stopped before the rest.');
});

test('the buttons say why they are off', () => {
  assert.equal(shotBlocker(view(), shot()), null);
  assert.match(shotBlocker(view({ may_change: false, can_change: false }), shot()), /^Only the person who made this video/);
  assert.equal(shotBlocker(view({ can_change: false, phase: 'locked', locked_reason: 'A run is going.' }), shot()),
    'A run is going.');
  assert.equal(shotBlocker(view(), shot({ problems: ['Lock the look of Wren before first frames are made'] })),
    'Lock the look of Wren before first frames are made');
  assert.equal(shotBlocker(view(), shot({ making: true })), 'Turns on once the options being made are ready.');
  assert.equal(optionName({ position: 0 }), 'Option 1');
});

test('the words on this screen keep to the copy rules', () => {
  const words = [
    inItLine(shot()), aboutLine(shot()), referencesLine(3), referencesLine(0), referencesLine(0, ['Lead']),
    referencesLine(2, ['Pip']), framesSummary({ shots: [shot()] }), priceStatus(dry),
    priceStatus(live), makeLabel(live), changesLine(['make it night']), roundTitle(round({ round: 2, changes: ['x'] })),
    roundNote(round({ status: 'failed', made: 0 })), changeBlocker(''), shotBlocker(view({ may_change: false }), shot()),
    shotBlocker(view(), shot({ making: true })), ...CHANGE_EXAMPLES,
    ...[shot(), shot({ making: true }), shot({ changed: true }), shot({ problems: ['p'] }), shot({ rounds: [round()] })]
      .map((s) => shotStatus(s).text),
  ];
  for (const text of words) {
    assert.deepEqual(bannedWords(text), [], text);
    assert.doesNotMatch(text, ICON_CHARACTERS, text);
    assert.doesNotMatch(text.toLowerCase(), /grok|soul|seedream|higgsfield|model|prompt/, text);
  }
});
