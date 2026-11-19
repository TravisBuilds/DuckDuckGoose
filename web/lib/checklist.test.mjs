import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ICON_CHARACTERS, bannedWords } from './copy-rules.mjs';
import {
  ADD_KINDS,
  KIND_LABELS,
  countsLine,
  itemPhotos,
  itemStatus,
  listMadeMessage,
  listReady,
  makeListBlocker,
  makeListLabel,
  otherPhotos,
  progressLine,
  progressPercent,
} from './checklist.mjs';

function item(extra = {}) {
  return {
    item_id: 'rc_1', kind: 'character', name: 'Mira', why: 'She finds the lantern', show: 'The face and the outfit',
    ideal_photos: 3, min_photos: 1, optional: false, source: 'story', photos: [], photo_count: 0, ready: false, ...extra,
  };
}

test('progress reads "4 of 7 ready", with the optional ones aside', () => {
  assert.equal(progressLine({ ready: 4, required: 7, optional: 0, optional_ready: 0 }), '4 of 7 ready');
  assert.equal(progressLine({ ready: 4, required: 7, optional: 2, optional_ready: 1 }), '4 of 7 ready · 1 of 2 optional');
  assert.equal(progressLine({ ready: 0, required: 0, optional: 2, optional_ready: 0 }),
    'Everything on the list is optional · 0 of 2 optional');
  assert.equal(progressLine({ ready: 0, required: 0, optional: 0, optional_ready: 0 }), 'Nothing on the list yet');
  assert.equal(progressLine(null), 'Nothing on the list yet');
  assert.equal(progressPercent({ ready: 4, required: 7 }), 57);
  assert.equal(progressPercent({ ready: 9, required: 7 }), 100);
  assert.equal(progressPercent({ ready: 0, required: 0 }), 0);
  assert.equal(listReady({ ready: 7, required: 7 }), true);
  assert.equal(listReady({ ready: 6, required: 7 }), false);
  assert.equal(listReady({ ready: 0, required: 0 }), false);
});

test('each item says how many photos are ideal and the fewest that work', () => {
  assert.equal(countsLine(item()), 'Ideally 3 photos, at least 1');
  assert.equal(countsLine(item({ ideal_photos: 2, min_photos: 2 })), '2 photos needed');
  assert.equal(countsLine(item({ ideal_photos: 1, min_photos: 0 })), '1 photo needed');
  assert.equal(countsLine(item({ optional: true, ideal_photos: 2 })), 'Optional · ideally 2 photos');
});

test('each item says where it stands', () => {
  assert.deepEqual(itemStatus(item()), { tone: 'needs', text: 'Needs 1 photo' });
  assert.deepEqual(itemStatus(item({ min_photos: 2 })), { tone: 'needs', text: 'Needs 2 photos' });
  assert.deepEqual(itemStatus(item({ photo_count: 1 })), { tone: 'ready', text: 'Ready · 2 more photos would help' });
  assert.deepEqual(itemStatus(item({ photo_count: 2 })), { tone: 'ready', text: 'Ready · 1 more photo would help' });
  assert.deepEqual(itemStatus(item({ photo_count: 3 })), { tone: 'ready', text: 'Ready' });
  assert.deepEqual(itemStatus(item({ optional: true })), { tone: 'optional', text: 'Optional' });
  assert.deepEqual(itemStatus(item({ optional: true, min_photos: 2, photo_count: 1 })), { tone: 'optional', text: '1 of 3 photos' });
});

test('the make button follows the story', () => {
  assert.equal(makeListLabel(null), 'Make my reference list');
  assert.equal(makeListLabel({ list_made: false, list_current: false }), 'Make my reference list');
  assert.equal(makeListLabel({ list_made: true, list_current: false }), 'Update the list from my story');
  assert.equal(makeListLabel({ list_made: true, list_current: true }), 'Make the list again');
  assert.equal(makeListBlocker({ saved: '', typed: 'Mira', storyMin: 20 }),
    'Turns on once your story has a few lines (at least 20 letters).');
  assert.equal(makeListBlocker({ saved: '', typed: 'Mira finds a lantern in the old lighthouse.' }), null);
  assert.equal(makeListBlocker({ saved: 'Mira finds a lantern in the old lighthouse.', typed: '' }), null);
});

test('photos on an item, and the ones on no item', () => {
  const photos = [{ image_id: 'ui_1' }, { image_id: 'ui_2' }, { image_id: 'ui_3' }];
  const checklist = { items: [item({ photos: ['ui_3', 'ui_1', 'ui_gone'] }), item({ item_id: 'rc_2', photos: [] })] };
  assert.deepEqual(itemPhotos(checklist.items[0], photos).map((p) => p.image_id), ['ui_3', 'ui_1']);
  assert.deepEqual(otherPhotos(checklist, photos).map((p) => p.image_id), ['ui_2']);
  assert.deepEqual(otherPhotos(undefined, photos).map((p) => p.image_id), ['ui_1', 'ui_2', 'ui_3']);
  assert.deepEqual(itemPhotos(null, photos), []);
});

test('making the list says what changed in a sentence', () => {
  assert.equal(listMadeMessage({ added: ['Mira', 'Tobin'], removed: [], dropped: [] }), 'Your list is ready: 2 references added.');
  assert.equal(listMadeMessage({ added: [], removed: ['Tobin'], dropped: ['Mask'] }),
    'Your list is ready: 1 the story no longer needs taken off. Left out what the studio cannot make: Mask.');
  assert.equal(listMadeMessage({ added: [], removed: [] }), 'Your list already matches the story.');
});

test('every word the list shows keeps to the copy rules', () => {
  const texts = [
    ...Object.values(KIND_LABELS), ...ADD_KINDS.map((k) => k.label),
    progressLine({ ready: 4, required: 7, optional: 2, optional_ready: 1 }), progressLine(null),
    ...[0, 1, 2, 3].flatMap((photo_count) => [itemStatus(item({ photo_count })).text, itemStatus(item({ photo_count, optional: true })).text]),
    countsLine(item()), countsLine(item({ optional: true })), countsLine(item({ ideal_photos: 2, min_photos: 2 })),
    makeListLabel(null), makeListLabel({ list_made: true }), makeListLabel({ list_made: true, list_current: true }),
    makeListBlocker({ saved: '', typed: '' }), listMadeMessage({ added: ['Mira'], removed: ['Tobin'], dropped: ['Mask'] }),
  ];
  for (const text of texts) {
    assert.deepEqual(bannedWords(text), [], text);
    assert.doesNotMatch(text, ICON_CHARACTERS, text);
  }
});
