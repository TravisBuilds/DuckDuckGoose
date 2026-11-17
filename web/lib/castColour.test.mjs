import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  CAST_COLOURS,
  NARRATOR,
  castColourAt,
  castColours,
  creationOrder,
  isNarrator,
  speakerColours,
} from './castColour.mjs';

const day = (n) => `2026-11-${String(n).padStart(2, '0')}T10:00:00Z`;

test('the first six characters get blue, mustard, aqua, coral, green and lilac in creation order', () => {
  assert.deepEqual(CAST_COLOURS, ['blue', 'mustard', 'aqua', 'coral', 'green', 'lilac']);
  const colours = [0, 1, 2, 3, 4, 5].map((index) => castColourAt(index));
  assert.deepEqual(colours.map((c) => c.colour), CAST_COLOURS);
  assert.ok(colours.every((c) => !c.patterned && !c.narrator));
});

test('after six the colours repeat with a pattern, so no two characters look alike', () => {
  const seventh = castColourAt(6);
  assert.equal(seventh.colour, 'blue');
  assert.equal(seventh.patterned, true);
  assert.equal(castColourAt(11).colour, 'lilac');
  assert.equal(castColourAt(11).patterned, true);
  assert.equal(castColourAt(12).colour, 'blue');
  const seen = new Set([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11].map((i) => `${castColourAt(i).colour}:${castColourAt(i).patterned}`));
  assert.equal(seen.size, 12);
});

test('a bad index is the narrator, never a character colour', () => {
  for (const bad of [-1, 1.5, NaN, undefined, null, '2']) assert.equal(castColourAt(bad), NARRATOR);
  assert.equal(NARRATOR.colour, 'ink');
});

test('colours follow created_at, not the order the list arrives in', () => {
  const characters = [
    { slug: 'oskar', name: 'Oskar Brine', created_at: day(3) },
    { slug: 'mira', name: 'Mira Vale', created_at: day(1) },
    { slug: 'pip', name: 'Pip', created_at: day(2) },
  ];
  const colours = castColours(characters);
  assert.equal(colours.mira.colour, 'blue');
  assert.equal(colours.pip.colour, 'mustard');
  assert.equal(colours.oskar.colour, 'aqua');
  assert.deepEqual(castColours([...characters].reverse()), colours);
});

test('same created_at falls back to slug, like the API order', () => {
  const characters = [
    { slug: 'zed', created_at: day(1) },
    { slug: 'abe', created_at: day(1) },
  ];
  assert.deepEqual(creationOrder(characters).map((c) => c.slug), ['abe', 'zed']);
  assert.equal(castColours(characters).abe.colour, 'blue');
  assert.equal(castColours(characters).zed.colour, 'mustard');
});

test('characters without a date keep their given order after the dated ones', () => {
  const characters = [{ slug: 'b' }, { slug: 'a' }, { slug: 'c', created_at: day(5) }];
  assert.deepEqual(creationOrder(characters).map((c) => c.slug), ['c', 'b', 'a']);
  assert.deepEqual(creationOrder([{ slug: 'z' }, { slug: 'y' }]).map((c) => c.slug), ['z', 'y']);
  assert.deepEqual(creationOrder(null), []);
});

test('the function is pure: the input list is not reordered', () => {
  const characters = [{ slug: 'b', created_at: day(2) }, { slug: 'a', created_at: day(1) }];
  castColours(characters);
  assert.deepEqual(characters.map((c) => c.slug), ['b', 'a']);
});

test('the narrator, or a line without a speaker, is ink', () => {
  for (const name of ['Narrator', ' narrator ', '', null, undefined, 'The narrator']) assert.ok(isNarrator(name));
  assert.ok(!isNarrator('Mira'));
  const colours = speakerColours(['Narrator', null], []);
  assert.equal(colours.Narrator, NARRATOR);
  assert.equal(colours[''], NARRATOR);
});

test('a speaker named like a Universe character gets that character\'s colour, ignoring case', () => {
  const characters = [
    { slug: 'mira', name: 'Mira Vale', created_at: day(1) },
    { slug: 'oskar', name: 'Oskar Brine', created_at: day(2) },
  ];
  const colours = speakerColours(['oskar brine', 'Mira Vale', 'Narrator'], characters);
  assert.equal(colours['Mira Vale'].colour, 'blue');
  assert.equal(colours['oskar brine'].colour, 'mustard');
  assert.equal(colours.Narrator.colour, 'ink');
});

test('other speakers take the next free colours in the order they first speak', () => {
  const characters = [{ slug: 'mira', name: 'Mira Vale', created_at: day(1) }];
  const colours = speakerColours(['Ferry hand', 'Mira Vale', 'Lamp keeper', 'Ferry hand'], characters);
  assert.equal(colours['Mira Vale'].colour, 'blue');
  assert.equal(colours['Ferry hand'].colour, 'mustard');
  assert.equal(colours['Lamp keeper'].colour, 'aqua');
  assert.equal(Object.keys(colours).length, 3);
});
