import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  CREATION_STEPS,
  boardUnlockHint,
  byNeedsYou,
  boardUnlocked,
  creationJourney,
  homeJourney,
  screenSteps,
  sealFor,
  stepFromQuery,
  stepNumber,
  toneChips,
} from './journey.mjs';

function view({ notes = 0, photos = 0, boards = [], bibleBoard = null, characters = [], items = [] } = {}) {
  return {
    world: { series_id: 'sr_1', profile: { bible_board_id: bibleBoard }, counts: {} },
    characters,
    items,
    creation: {
      notes: Array.from({ length: notes }, (_, i) => ({ note_id: `un_${i}` })),
      photos: Array.from({ length: photos }, (_, i) => ({ image_id: `ui_${i}` })),
      boards,
    },
  };
}

const states = (journey) => journey.steps.map((s) => `${s.key}:${s.state}`);

test('the mood board opens at one note or three photos', () => {
  assert.equal(boardUnlocked(0, 0), false);
  assert.equal(boardUnlocked(0, 2), false);
  assert.equal(boardUnlocked(0, 3), true);
  assert.equal(boardUnlocked(1, 0), true);
  assert.equal(boardUnlockHint(0, 1), 'Add a note, or 2 more photos, to open the mood board.');
  assert.equal(boardUnlockHint(0, 2), 'Add a note, or 1 more photo, to open the mood board.');
  assert.equal(boardUnlockHint(1, 0), null);
});

test('a new Universe is on References: Spark is done the moment it has a name', () => {
  const journey = creationJourney(view());
  assert.deepEqual(states(journey), ['spark:done', 'references:now', 'moodboard:todo', 'bible:todo', 'lock:todo']);
  assert.equal(journey.current, 'references');
  assert.equal(journey.boardOpen, false);
});

test('a note opens the mood board step', () => {
  const journey = creationJourney(view({ notes: 1 }));
  assert.equal(journey.current, 'moodboard');
  assert.equal(journey.steps[1].note, '0 photos · 1 note');
  assert.equal(journey.steps[2].note, 'Make & approve');
});

test('a board waiting for a look, then approved, then the bible', () => {
  const ready = { board_id: 'mb_1', version: 1, status: 'ready' };
  assert.equal(creationJourney(view({ notes: 2, boards: [ready] })).steps[2].note, 'Board 1 to look over');
  const approved = { ...ready, status: 'approved' };
  const afterApprove = creationJourney(view({ notes: 2, boards: [approved] }));
  assert.equal(afterApprove.current, 'bible');
  assert.equal(afterApprove.steps[2].note, 'Board 1 approved');
  const characters = [{ slug: 'a', locked_versions: [] }, { slug: 'b', locked_versions: [1] }];
  const written = creationJourney(view({ notes: 2, boards: [approved], bibleBoard: 'mb_1', characters, items: [{}, {}, {}] }));
  assert.equal(written.current, 'lock');
  assert.equal(written.steps[3].note, '2 characters · 3 places/props');
  assert.equal(written.steps[4].note, '1 of 2 sealed');
});

test('every look locked: all five steps done and no current step', () => {
  const approved = { board_id: 'mb_1', version: 1, status: 'approved' };
  const characters = [{ slug: 'a', locked_versions: [1] }];
  const journey = creationJourney(view({ notes: 1, boards: [approved], bibleBoard: 'mb_1', characters }));
  assert.equal(journey.current, null);
  assert.ok(journey.steps.every((s) => s.state === 'done'));
});

test('a board already made keeps References done even if references were removed since', () => {
  const journey = creationJourney(view({ boards: [{ board_id: 'mb_1', version: 1, status: 'ready' }] }));
  assert.equal(journey.steps[1].state, 'done');
  assert.equal(journey.current, 'moodboard');
});

test('a creation screen highlights itself, not the next step, and shows finished steps done', () => {
  const screen = (journey, key) => screenSteps(journey, key).map((s) => `${s.key}:${s.state}${s.complete ? '+' : ''}`);
  // References done (a note opened the board), the Universe's next step is the mood board
  const open = creationJourney(view({ notes: 1 }));
  assert.equal(open.current, 'moodboard');
  assert.deepEqual(screen(open, 'references'), ['spark:done+', 'references:now+', 'moodboard:todo', 'bible:todo', 'lock:todo']);
  assert.deepEqual(screen(open, 'moodboard'), ['spark:done+', 'references:done+', 'moodboard:now', 'bible:todo', 'lock:todo']);
  // looking back at the spark of a Universe with its bible written: the later steps stay done
  const approved = { board_id: 'mb_1', version: 1, status: 'approved' };
  const written = creationJourney(view({ notes: 1, boards: [approved], bibleBoard: 'mb_1', characters: [{ slug: 'a', locked_versions: [] }] }));
  assert.deepEqual(screen(written, 'spark'), ['spark:now+', 'references:done+', 'moodboard:done+', 'bible:done+', 'lock:todo']);
  assert.deepEqual(screen(written, 'lock'), ['spark:done+', 'references:done+', 'moodboard:done+', 'bible:done+', 'lock:now']);
  // the notes stay the journey's
  assert.deepEqual(screenSteps(open, 'references').map((s) => s.note), open.steps.map((s) => s.note));
  assert.deepEqual(screenSteps(null, 'spark'), []);
});

test('a step in the URL wins; anything else is the current step', () => {
  const journey = { current: 'moodboard' };
  for (const key of CREATION_STEPS) assert.equal(stepFromQuery(key, journey), key);
  assert.equal(stepFromQuery('MOODBOARD', journey), 'moodboard');
  assert.equal(stepFromQuery('', journey), 'moodboard');
  assert.equal(stepFromQuery('nope', journey), 'moodboard');
  assert.equal(stepFromQuery(null, { current: null }), 'lock');
  assert.equal(stepNumber('spark'), 1);
  assert.equal(stepNumber('lock'), 5);
});

function card(extra = {}) {
  return {
    can_edit: true, board_status: null, profile: { bible_board_id: null },
    counts: { characters: 0, locked_characters: 0 }, ...extra,
  };
}

test('home: a fresh Universe asks for references', () => {
  const journey = homeJourney(card());
  assert.equal(journey.current, 'references');
  assert.equal(journey.step, 2);
  assert.deepEqual(journey.next, { text: 'Next: add photos and notes', tone: 'needs', step: 'references' });
});

test('home: the board status says what the mood board step needs', () => {
  assert.equal(homeJourney(card({ board_status: 'ready' })).next.text, 'Next: look over your mood board');
  assert.equal(homeJourney(card({ board_status: 'making' })).next.tone, 'working');
  assert.equal(homeJourney(card({ board_status: 'failed' })).next.text, 'Next: try the mood board again');
  assert.equal(homeJourney(card({ board_status: 'approved' })).next.text, 'Next: write the bible');
});

test('home: after the bible, the looks to lock; then ready for episodes', () => {
  const bible = { board_status: 'approved', profile: { bible_board_id: 'mb_1' } };
  const two = homeJourney(card({ ...bible, counts: { characters: 3, locked_characters: 1 } }));
  assert.equal(two.current, 'lock');
  assert.equal(two.next.text, 'Next: lock 2 more looks');
  const one = homeJourney(card({ ...bible, counts: { characters: 2, locked_characters: 1 } }));
  assert.equal(one.next.text, 'Next: lock 1 more look');
  const done = homeJourney(card({ ...bible, counts: { characters: 2, locked_characters: 2 } }));
  assert.equal(done.current, null);
  assert.equal(done.step, 5);
  assert.deepEqual(done.next, { text: 'Ready: make an episode', tone: 'ok', step: null });
});

function storyView({ written = false, items = [], ...rest } = {}) {
  const base = view(rest);
  const required = items.filter((i) => !i.optional);
  base.creation.story = { body: written ? 'Mira finds a lantern.' : '', written, list_made: items.length > 0, list_current: true };
  base.creation.checklist = {
    items,
    progress: {
      ready: required.filter((i) => i.ready).length, required: required.length,
      optional: items.length - required.length, optional_ready: items.filter((i) => i.optional && i.ready).length,
      photos: 0,
    },
  };
  return base;
}

test('story first: a new Universe asks for its story, and a written story opens the mood board', () => {
  const fresh = creationJourney(storyView());
  assert.equal(fresh.current, 'references');
  assert.equal(fresh.steps[1].name, 'Story & photos');
  assert.equal(fresh.steps[1].note, 'Story first');
  assert.equal(fresh.story, false);
  assert.equal(fresh.boardOpen, false);
  assert.equal(boardUnlockHint(0, 0, false), 'Write your story first: it opens the mood board.');
  const written = creationJourney(storyView({ written: true }));
  assert.equal(written.current, 'moodboard');
  assert.equal(written.steps[1].note, 'Story written');
  assert.equal(written.boardOpen, true);
  assert.equal(boardUnlocked(0, 0, true), true);
  assert.equal(boardUnlockHint(0, 0, true), null);
});

test('story first: the reference list shows how many are ready, optional ones aside', () => {
  const items = [
    { item_id: 'rc_1', ready: true, optional: false }, { item_id: 'rc_2', ready: false, optional: false },
    { item_id: 'rc_3', ready: true, optional: false }, { item_id: 'rc_4', ready: false, optional: true },
  ];
  assert.equal(creationJourney(storyView({ written: true, items })).steps[1].note, '2 of 3 ready');
});

test('story first: a Universe from before stories keeps its notes and photos path', () => {
  const older = creationJourney(storyView({ notes: 1, photos: 2 }));
  assert.equal(older.current, 'moodboard');
  assert.equal(older.steps[1].note, '2 photos · 1 note');
  assert.equal(creationJourney(view({ photos: 2 })).story, undefined);
  assert.equal(boardUnlockHint(0, 1, false), 'Write your story first: it opens the mood board.');
});

test('home: a fresh Universe asks for its story once the API knows stories', () => {
  assert.deepEqual(homeJourney(card({ story_written: false })).next,
    { text: 'Next: write your story', tone: 'needs', step: 'references' });
  const written = homeJourney(card({ story_written: true }));
  assert.equal(written.current, 'moodboard');
  assert.equal(written.next.text, 'Next: make your mood board');
  // an API from before stories, or a card someone else sees, reads as before
  assert.equal(homeJourney(card({ story_written: null })).next.text, 'Next: add photos and notes');
});

test('home: someone who only sees a Universe gets no progress, just a way in', () => {
  const journey = homeJourney(card({ can_edit: false }));
  assert.equal(journey.steps, null);
  assert.equal(journey.next.text, 'Open the Universe');
});

test('the seal: open until a look is locked, then how many, coral when a look changed', () => {
  assert.deepEqual(sealFor({ characters: 0, locked_characters: 0 }), { state: 'open', top: 'not', bottom: 'locked' });
  assert.deepEqual(sealFor({ characters: 3, locked_characters: 0 }), { state: 'open', top: 'not', bottom: 'locked' });
  assert.deepEqual(sealFor({ characters: 3, locked_characters: 2 }), { state: 'locked', top: '2 of 3', bottom: 'locked' });
  assert.equal(sealFor({ characters: 3, locked_characters: 3 }, 1).state, 'drift');
});

test('tone chips: short phrases from a tone note, no repeats, at most six', () => {
  assert.deepEqual(toneChips('Warm and gentle, rainy evenings, amber light'), ['Warm and gentle', 'rainy evenings', 'amber light']);
  assert.deepEqual(toneChips('cosy; cosy. Cosy\nsoft'), ['cosy', 'soft']);
  assert.deepEqual(toneChips('a, b, c, d, e, f, g, h').length, 6);
  assert.deepEqual(toneChips('this phrase is far too long to be a little tone chip at all, short'), ['short']);
  assert.deepEqual(toneChips(null), []);
});

test('keep going: a decision waiting for you comes first, then stopped, then working; ties keep their order', () => {
  const entries = [
    { id: 'a', tone: 'wait' }, { id: 'b', tone: 'todo' }, { id: 'c', tone: 'ok' },
    { id: 'd', tone: 'bad' }, { id: 'e', tone: 'todo' }, { id: 'f', tone: null }, { id: 'g', tone: 'run' },
  ];
  assert.deepEqual(byNeedsYou(entries, (e) => e.tone).map((e) => e.id), ['b', 'e', 'd', 'g', 'a', 'c', 'f']);
  assert.deepEqual(byNeedsYou(null, () => 'todo'), []);
});
