/**
 * First frames (docs/CONCEPT-FRAMES.md): the words the episode page shows for each shot's start frame
 * options. Pure functions over what GET /api/episodes/{id}/concept-frames and its estimate return, so
 * the page and the tests agree. No model or prompt ever reaches the page: the studio picks the
 * picture model and writes the prompt.
 */

import { formatUsdMicros } from './balance.mjs';
import { promise, quote } from './checkout.mjs';

/** Plain-words changes people ask for most, offered as examples (each one is a button that fills the box). */
export const CHANGE_EXAMPLES = ['make it night', 'closer on her face', 'show the whole room', 'warmer colours'];

export const CHANGE_MAX = 200;

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** "A, B and C". */
export function listWords(words) {
  const list = (words ?? []).filter(Boolean);
  return list.length <= 1 ? list.join('') : `${list.slice(0, -1).join(', ')} and ${list[list.length - 1]}`;
}

/** Who and what the start frame shows: "Wren and Pip at Harbour cabin, with Brass key". */
export function inItLine(shot) {
  const who = listWords(shot?.characters);
  const where = shot?.place ? `at ${shot.place}` : '';
  const things = listWords(shot?.props);
  let line = [who, where].filter(Boolean).join(' ');
  if (things) line = line ? `${line}, with ${things}` : `with ${things}`;
  if (!line) return 'No character, place or prop named in this shot';
  return line.charAt(0).toUpperCase() + line.slice(1);
}

/** The shot's first frame in plain words, as the studio describes it, else who and what is in it. */
export function aboutLine(shot) {
  const text = (shot?.description ?? '').trim();
  return text || inItLine(shot);
}

/**
 * The shot's reference photos, said once: "3 reference photos guide it", "2 reference photos guide it;
 * none of Pip yet", "No reference photos of Lead yet: the words guide it".
 */
export function referencesLine(count, missing = []) {
  const names = listWords(missing);
  if (count > 0) {
    const line = `${plural(count, 'reference photo')} ${count === 1 ? 'guides' : 'guide'} it`;
    return names ? `${line}; none of ${names} yet` : line;
  }
  return names ? `No reference photos of ${names} yet: the words guide it` : 'No reference photos yet: the words guide it';
}

/** The newest round, or null. */
export function latestRound(shot) {
  return shot?.rounds?.[0] ?? null;
}

/**
 * Where a shot stands, for the line under its title: picked (lime), being made (cyan), stopped by a
 * problem (orange-red), changed since its options (yellow, a decision), or not picked yet (plain:
 * first frames are optional).
 */
export function shotStatus(shot) {
  if (shot?.pick?.source === 'shot_list') return { tone: 'ok', text: 'Start frame set in the shot list' };
  if (shot?.pick?.stale) return { tone: 'todo', text: 'Start frame picked, but the shot changed since' };
  if (shot?.pick) return { tone: 'ok', text: 'Start frame locked' };
  if (shot?.making) return { tone: 'run', text: 'Making options now' };
  if ((shot?.problems ?? []).length > 0) return { tone: 'bad', text: 'Waiting on something first' };
  if (shot?.changed) return { tone: 'todo', text: 'The shot changed: make new options' };
  const latest = latestRound(shot);
  if (latest && latest.made > 0) return { tone: 'todo', text: 'Options ready: pick one' };
  return { tone: 'wait', text: 'No start frame yet (optional)' };
}

/** How many shots have a start frame, and how many there are. */
export function framesCount(view) {
  const shots = view?.shots ?? [];
  return { picked: shots.filter((s) => s.pick).length, shots: shots.length };
}

/** "Start frames picked: 2 of 5 shots". */
export function framesSummary(view) {
  const { picked, shots } = framesCount(view);
  return shots === 0 ? 'No shots yet' : `Start frames picked: ${picked} of ${plural(shots, 'shot')}`;
}

/** The price after "3 options:" in the checkout: "free" in a test run, else "about" the provider's price. */
export function priceTotal(estimate) {
  return quote(estimate)?.words ?? '';
}

/** The one calm line under the make button (the server re-checks the approved total when it makes them). */
export function priceStatus(estimate) {
  return promise(quote(estimate)) ?? '';
}

/** "3 options", what the checkout prices. */
export function priceWhat(estimate) {
  return plural(estimate?.count ?? 0, 'option');
}

/** The lines behind "See the breakdown". */
export function priceRows(estimate) {
  if (!estimate) return [];
  const each = estimate.per_image_usd_micros?.[0] ?? 0;
  const rows = [{ label: plural(estimate.count, 'option'), value: estimate.dry_run ? 'Free stand-ins' : `${formatUsdMicros(each)} each` }];
  if (!estimate.dry_run && estimate.budget) {
    rows.push({ label: 'Left in the budget for first frames', value: formatUsdMicros(estimate.budget.available_usd_micros) });
  }
  return rows;
}

/** The make button's words: "Make 3 options" (the price is said once, above it). */
export function makeLabel(estimate) {
  if (!estimate) return 'Make options';
  return `Make ${plural(estimate.count, 'option')}`;
}

/** "Changes so far: make it night; closer on her face", or null. */
export function changesLine(changes) {
  const parts = changesParts(changes);
  return parts ? `${parts.label} ${parts.text}` : null;
}

/** The same, as its label and the changes: the page keeps the label on one line. */
export function changesParts(changes) {
  const list = (changes ?? []).filter(Boolean);
  return list.length > 0 ? { label: 'Changes so far:', text: list.join('; ') } : null;
}

/** A round's heading: "Try 3, with make it night" or "First try". */
export function roundTitle(round) {
  if (!round) return '';
  const name = round.round === 1 ? 'First try' : `Try ${round.round}`;
  const changes = round.changes ?? [];
  return changes.length > 0 ? `${name}, with ${changes[changes.length - 1]}` : name;
}

/** What happened to a round that did not make every option, in plain words, or null. */
export function roundNote(round) {
  if (!round) return null;
  if (round.status === 'making') {
    return round.made > 0 ? `${round.made} of ${round.option_count} made so far` : 'The studio is making them now';
  }
  if (round.status === 'ready') return null;
  const made = round.made > 0 ? `${round.made} of ${round.option_count} made. ` : 'None were made. ';
  return `${made}${round.error || 'The studio stopped before the rest.'}`;
}

/** Why the change box's button is off, or null when it is on. */
export function changeBlocker(text) {
  const words = (text ?? '').trim();
  if (!words) return 'Turns on once you say what to change.';
  if (words.length > CHANGE_MAX) return `Keep it under ${CHANGE_MAX} letters.`;
  return null;
}

/**
 * Why a shot's buttons are off, or null when the reader may make and pick its first frame now:
 * not theirs to change, a run going, the shot not ready, or a round being made.
 */
export function shotBlocker(view, shot) {
  if (!view?.may_change) return 'Only the person who made this video, or an admin, can pick its first frames.';
  if (!view.can_change) return view.locked_reason || 'First frames are picked before a video starts filming.';
  if ((shot?.problems ?? []).length > 0) return shot.problems[0];
  if (shot?.making) return 'Turns on once the options being made are ready.';
  return null;
}

/** An option's name for buttons and pictures: "Option 2". */
export function optionName(option) {
  return `Option ${(option?.position ?? 0) + 1}`;
}

/** The option a shot starts from now (picked here), or null. */
export function pickedOption(shot) {
  return shot?.pick?.source === 'here' ? shot.pick.option_id : null;
}

/** Whether the page should check again soon: a round is being made. */
export function anyMaking(view) {
  return (view?.shots ?? []).some((s) => s.making);
}
