/**
 * Cast colours (DESIGN-TARGETS-v2 §1.4). Each Universe character gets one by creation order, from
 * blue, mustard, aqua, coral, green and lilac. After six the order repeats with a pattern (a striped
 * dot), so two characters never look alike. The narrator is always ink.
 *
 * There is no stored colour field yet, so the colour is derived: the same characters in the same
 * creation order always get the same colours, on every screen and for every viewer.
 */

export const CAST_COLOURS = ['blue', 'mustard', 'aqua', 'coral', 'green', 'lilac'];

export const NARRATOR = Object.freeze({ colour: 'ink', patterned: false, index: -1, narrator: true });

/** The colour for the character at position `index` in creation order (0 = first made). */
export function castColourAt(index) {
  if (!Number.isInteger(index) || index < 0) return NARRATOR;
  return {
    colour: CAST_COLOURS[index % CAST_COLOURS.length],
    patterned: index >= CAST_COLOURS.length,
    index,
    narrator: false,
  };
}

function stamp(value) {
  const time = Date.parse(value ?? '');
  return Number.isNaN(time) ? Number.POSITIVE_INFINITY : time;
}

/**
 * Characters in creation order: created_at, then slug (the API's own order). Characters without a
 * created_at keep the order they were given in, after the dated ones.
 */
export function creationOrder(characters) {
  return (characters ?? [])
    .map((character, position) => ({ character, position }))
    .sort((a, b) => {
      const ta = stamp(a.character?.created_at);
      const tb = stamp(b.character?.created_at);
      if (ta !== tb) return ta < tb ? -1 : 1;
      if (Number.isFinite(ta)) {
        const bySlug = String(a.character?.slug ?? '').localeCompare(String(b.character?.slug ?? ''));
        if (bySlug !== 0) return bySlug;
      }
      return a.position - b.position;
    })
    .map(({ character }) => character);
}

/** slug -> colour for one Universe's characters. */
export function castColours(characters) {
  const out = {};
  creationOrder(characters).forEach((character, index) => {
    if (character?.slug != null && !(character.slug in out)) out[character.slug] = castColourAt(index);
  });
  return out;
}

const normal = (name) => String(name ?? '').trim().toLowerCase();

export function isNarrator(speaker) {
  const name = normal(speaker);
  return name === '' || name === 'narrator' || name === 'the narrator';
}

/**
 * name -> colour for the speakers of a script. A speaker named like a Universe character gets that
 * character's colour; any other speaker gets the next free colour, in the order they first speak;
 * the narrator (or a line without a speaker) is ink.
 */
export function speakerColours(speakers, characters = []) {
  const ordered = creationOrder(characters);
  const byName = new Map();
  ordered.forEach((character, index) => {
    const key = normal(character?.name);
    if (key && !byName.has(key)) byName.set(key, castColourAt(index));
  });
  let next = ordered.length;
  const out = {};
  for (const speaker of speakers ?? []) {
    const label = speaker ?? '';
    if (label in out) continue;
    if (isNarrator(label)) out[label] = NARRATOR;
    else if (byName.has(normal(label))) out[label] = byName.get(normal(label));
    else out[label] = castColourAt(next++);
  }
  return out;
}
