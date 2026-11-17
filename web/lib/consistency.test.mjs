import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  autoMapCast, castMatchText, characterBadge, characterSummaryBadge, countsLine, driftCount, healthLine, itemBadge,
  sameName, stripBadge, usedInLabel,
} from './consistency.mjs';

const v = (version, locked, problems = []) => ({ version, locked, problems });

test('a character version reads locked, not locked yet, older look or look changed since lock', () => {
  assert.deepEqual(characterBadge(v(2, true), 2), { key: 'locked', label: 'Locked v2', tone: 'ok' });
  assert.deepEqual(characterBadge(v(1, true)), { key: 'locked', label: 'Locked v1', tone: 'ok' });
  assert.deepEqual(characterBadge(v(1, false)), { key: 'unlocked', label: 'Not locked yet', tone: 'wait' });
  assert.deepEqual(characterBadge(null), { key: 'unlocked', label: 'Not locked yet', tone: 'wait' });
  // an episode that uses v2 while a newer locked v3 exists
  assert.deepEqual(characterBadge(v(2, true), 3), { key: 'older', label: 'Older look', tone: 'warn' });
  assert.deepEqual(characterBadge(v(1, false), 2).label, 'Older look');
  // a lock whose pictures changed is the first thing to say, even on an older look
  const drift = { key: 'drift', label: 'Look changed since lock', tone: 'bad' };
  assert.deepEqual(characterBadge(v(3, true, ['master.png changed']), 3), drift);
  assert.deepEqual(characterBadge(v(2, true, ['master.png changed']), 3), drift);
  // problems of a version that is not locked mean nothing yet
  assert.equal(characterBadge(v(1, false, ['x']), null).label, 'Not locked yet');
});

test('a character card shows its newest locked look, and a strip entry the version the episode uses', () => {
  assert.equal(characterSummaryBadge({ versions: [v(1, true), v(2, false), v(3, true)] }).label, 'Locked v3');
  assert.equal(characterSummaryBadge({ versions: [v(1, false)] }).label, 'Not locked yet');
  assert.equal(characterSummaryBadge({ versions: [v(1, true), v(2, true, ['changed'])] }).label, 'Look changed since lock');
  assert.equal(characterSummaryBadge(null).label, 'Not locked yet');
  assert.equal(stripBadge({ version: 2, locked: true, latest_locked: 2, problems: [] }).label, 'Locked v2');
  assert.equal(stripBadge({ version: 1, locked: true, latest_locked: 2, problems: [] }).label, 'Older look');
  assert.equal(stripBadge({ version: 1, locked: false, latest_locked: null, problems: [] }).label, 'Not locked yet');
});

test('a prop or place reads Photo set or No photo', () => {
  assert.deepEqual(itemBadge({ photos: [{ image_id: 'pi_1' }] }), { key: 'photo', label: 'Photo set', tone: 'ok' });
  assert.deepEqual(itemBadge({ photos: 2 }).label, 'Photo set');
  assert.deepEqual(itemBadge({ photos: [], primary_photo: null }), { key: 'no-photo', label: 'No photo', tone: 'wait' });
  assert.equal(itemBadge({ photos: 0 }).label, 'No photo');
  assert.equal(itemBadge({ photos: 0, image: { url: '/x' } }).label, 'Photo set');
  assert.equal(itemBadge(null).label, 'No photo');
});

test('the health line counts locked characters and looks that changed since their lock', () => {
  assert.equal(healthLine({ characters: 4, locked_characters: 3 }), '3 of 4 characters locked');
  assert.equal(healthLine({ characters: 1, locked_characters: 0 }), '0 of 1 character locked');
  assert.equal(healthLine({ characters: 0, locked_characters: 0 }), 'No characters yet');
  assert.equal(healthLine(null), 'No characters yet');
  assert.equal(healthLine({ characters: 4, locked_characters: 4 }, 1), '4 of 4 characters locked · 1 look changed since lock');
  assert.equal(healthLine({ characters: 2, locked_characters: 2 }, 2), '2 of 2 characters locked · 2 looks changed since lock');
  const characters = [{ versions: [v(1, true, ['a']), v(2, true)] }, { versions: [v(1, false, ['b'])] }, {}];
  assert.equal(driftCount(characters), 1);
  assert.equal(driftCount(null), 0);
});

test('counts and used-in labels', () => {
  assert.equal(countsLine({ characters: 4, places: 2, props: 1, episodes: 5 }), '4 characters · 2 places · 1 prop · 5 episodes');
  assert.equal(countsLine({}), '0 characters · 0 places · 0 props · 0 episodes');
  assert.equal(usedInLabel(0), 'Not used in an episode yet');
  assert.equal(usedInLabel({ episodes: 1 }), 'Used in 1 episode');
  assert.equal(usedInLabel({ episodes: 3, versions: { 1: 3 } }), 'Used in 3 episodes');
  assert.equal(usedInLabel(null), 'Not used in an episode yet');
});

test('drafted characters map to the Universe characters with the same name, ignoring case and spaces around it', () => {
  const universe = [{ slug: 'mara', name: 'Mara' }, { slug: 'old-captain', name: 'Old Captain' }, { slug: 'wren', name: 'Wren' }];
  const rows = [
    { code: 'A', name: '  mara ' }, { code: 'B', name: 'OLD CAPTAIN' }, { code: 'C', name: 'Theo' }, { code: 'D', name: '' },
  ];
  assert.deepEqual(autoMapCast(rows, universe), { A: 'mara', B: 'old-captain' });
  // every drafted name matches: nothing is left unmapped
  const all = [{ code: 'A', name: 'Wren' }, { code: 'B', name: 'mara' }];
  assert.equal(Object.keys(autoMapCast(all, universe)).length, all.length);
  // no Universe, or no draft: nothing maps
  assert.deepEqual(autoMapCast(rows, []), {});
  assert.deepEqual(autoMapCast(null, universe), {});
  // a name two Universe characters share is the creator's choice; so is a second drafted "Mara"
  assert.deepEqual(autoMapCast([{ code: 'A', name: 'Sam' }], [{ slug: 'sam', name: 'Sam' }, { slug: 'sam-2', name: 'sam' }]), {});
  assert.deepEqual(autoMapCast([{ code: 'A', name: 'Mara' }, { code: 'B', name: 'mara' }], universe), { A: 'mara' });
  // a part of a name is not the name
  assert.deepEqual(autoMapCast([{ code: 'A', name: 'Mar' }, { code: 'B', name: 'Mara Lee' }], universe), {});
  assert.equal(sameName('  Old Captain '), 'old captain');
});

test('the wizard names the Universe character a drafted one is, with the look a new episode uses', () => {
  const mara = { name: 'Mara', default_version: 2, versions: [v(1, false), v(2, true)] };
  assert.deepEqual(castMatchText(mara), {
    matched: true, text: 'Same as in the Universe: Mara · Locked v2', badge: { key: 'locked', label: 'Locked v2', tone: 'ok' },
  });
  const wren = { name: 'Wren', default_version: 1, versions: [v(1, false)] };
  assert.equal(castMatchText(wren).text, 'Same as in the Universe: Wren · Not locked yet');
  // the first locked look is what a shot without a version uses; a newer locked outfit makes it an older look
  const theo = { name: 'Theo', default_version: 1, versions: [v(1, true), v(2, true)] };
  assert.equal(castMatchText(theo).text, 'Same as in the Universe: Theo · Older look');
  assert.deepEqual(castMatchText(null), { matched: false, text: 'New to this Universe', badge: null });
});
