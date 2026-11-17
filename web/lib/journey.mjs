/**
 * The five steps of making a Universe (DESIGN-TARGETS-v2 §6.3): Spark, References, Mood board,
 * Bible, Lock. Pure functions over what the API already returns, so the home card, the creation
 * route (/studio/universes/[id]/create?step=…) and the Universe page agree on where a Universe is.
 *
 * Nothing here spends or asks for anything: opening a step only reads.
 */

export const CREATION_STEPS = ['spark', 'references', 'moodboard', 'bible', 'lock'];

export const CREATION_NAMES = {
  spark: 'Spark',
  references: 'References',
  moodboard: 'Mood board',
  bible: 'Bible',
  lock: 'Lock',
};

/** The mood board opens once there are at least three photos or one note (§6.3). */
export const BOARD_MIN_PHOTOS = 3;

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

export function boardUnlocked(notes, photos) {
  return (notes ?? 0) >= 1 || (photos ?? 0) >= BOARD_MIN_PHOTOS;
}

/** What still opens the mood board, or null once it is open. */
export function boardUnlockHint(notes, photos) {
  if (boardUnlocked(notes, photos)) return null;
  const more = BOARD_MIN_PHOTOS - (photos ?? 0);
  return `Add a note, or ${plural(more, 'more photo')}, to open the mood board.`;
}

/**
 * Where a Universe being made stands, from GET /api/worlds/{id} (its creation part, the owner's and
 * admins' view). Each step is done, now (the first one not done) or todo.
 */
export function creationJourney(view) {
  const creation = view?.creation ?? null;
  const notes = creation?.notes?.length ?? 0;
  const photos = creation?.photos?.length ?? 0;
  const boards = creation?.boards ?? [];
  const latest = boards.length > 0 ? boards[boards.length - 1] : null;
  const approved = boards.find((b) => b.status === 'approved') ?? null;
  const bibleDone = Boolean(approved && view?.world?.profile?.bible_board_id === approved.board_id);
  const counts = view?.world?.counts ?? {};
  const characters = view?.characters ? view.characters.length : counts.characters ?? 0;
  const locked = view?.characters
    ? view.characters.filter((c) => (c.locked_versions ?? []).length > 0).length
    : counts.locked_characters ?? 0;
  const things = view?.items ? view.items.length : (counts.places ?? 0) + (counts.props ?? 0);
  const done = {
    spark: true,
    // a board made from the references counts them done, even if some were removed since
    references: boardUnlocked(notes, photos) || latest !== null,
    moodboard: approved !== null,
    bible: bibleDone,
    lock: characters > 0 && locked >= characters,
  };
  const current = CREATION_STEPS.find((key) => !done[key]) ?? null;
  const notesFor = {
    spark: 'Name & idea',
    references: notes + photos > 0 ? `${plural(photos, 'photo')} · ${plural(notes, 'note')}` : 'Photos & notes',
    moodboard: approved ? `Board ${approved.version} approved`
      : latest ? `Board ${latest.version} to look over` : current === 'moodboard' ? 'Make & approve' : 'After references',
    bible: bibleDone ? `${plural(characters, 'character')} · ${things} places/props`
      : current === 'bible' ? 'Write it' : 'After approval',
    lock: characters === 0 ? 'Seal the looks' : `${locked} of ${characters} sealed`,
  };
  const steps = CREATION_STEPS.map((key) => ({
    key,
    name: CREATION_NAMES[key],
    note: notesFor[key],
    state: done[key] ? 'done' : key === current ? 'now' : 'todo',
  }));
  return {
    steps,
    current,
    counts: { notes, photos, boards: boards.length, characters, locked, things },
    latest,
    approved,
    bibleDone,
    boardOpen: boardUnlocked(notes, photos),
  };
}

/** The step a creation URL asks for, or the Universe's current step (Lock once everything is done). */
export function stepFromQuery(raw, journey) {
  const asked = String(raw ?? '').toLowerCase();
  if (CREATION_STEPS.includes(asked)) return asked;
  return journey?.current ?? 'lock';
}

export function stepNumber(key) {
  return CREATION_STEPS.indexOf(key) + 1;
}

/**
 * A home card's progress from GET /api/worlds (no reference counts there): the five steps, the step
 * it is on, and what comes next. Only a Universe's owner and admins see its board status and bible;
 * for everyone else the card shows what the Universe keeps instead of where it is.
 */
export function homeJourney(card) {
  const counts = card?.counts ?? {};
  const characters = counts.characters ?? 0;
  const locked = counts.locked_characters ?? 0;
  if (!card?.can_edit) {
    return { steps: null, current: null, step: 0, next: { text: 'Open the Universe', tone: 'go', step: null } };
  }
  const status = card.board_status ?? null;
  const bibleDone = Boolean(card.profile?.bible_board_id);
  const boardDone = status === 'approved' || bibleDone;
  const done = {
    spark: true,
    // the list has no reference counts: a board, a bible or a character means references were added
    references: status !== null || boardDone || characters > 0,
    moodboard: boardDone,
    bible: bibleDone,
    lock: characters > 0 && locked >= characters,
  };
  const current = CREATION_STEPS.find((key) => !done[key]) ?? null;
  const steps = CREATION_STEPS.map((key) => ({
    key, name: CREATION_NAMES[key], state: done[key] ? 'done' : key === current ? 'now' : 'todo',
  }));
  const step = current ? stepNumber(current) : CREATION_STEPS.length;
  let next;
  switch (current) {
    case 'references':
      next = { text: 'Next: add photos and notes', tone: 'needs', step: 'references' };
      break;
    case 'moodboard':
      next = status === 'making' ? { text: 'Your mood board is being made', tone: 'working', step: 'moodboard' }
        : status === 'ready' ? { text: 'Next: look over your mood board', tone: 'needs', step: 'moodboard' }
          : status === 'failed' ? { text: 'Next: try the mood board again', tone: 'needs', step: 'moodboard' }
            : { text: 'Next: make your mood board', tone: 'needs', step: 'moodboard' };
      break;
    case 'bible':
      next = { text: 'Next: write the bible', tone: 'needs', step: 'bible' };
      break;
    case 'lock':
      next = characters === 0
        ? { text: 'Next: add your characters', tone: 'needs', step: 'lock' }
        : { text: `Next: lock ${plural(characters - locked, 'more look')}`, tone: 'needs', step: 'lock' };
      break;
    default:
      next = { text: 'Ready: make an episode', tone: 'ok', step: null };
  }
  return { steps, current, step, next };
}

/** The seal on a card or banner: locked once any look is, open before; the words say how many. */
export function sealFor(counts, drift = 0) {
  const characters = counts?.characters ?? 0;
  const locked = counts?.locked_characters ?? 0;
  if (drift > 0) return { state: 'drift', top: 'look', bottom: 'changed' };
  if (characters === 0 || locked === 0) return { state: 'open', top: 'not', bottom: 'locked' };
  return { state: 'locked', top: `${locked} of ${characters}`, bottom: 'locked' };
}

/** Short tone chips from a free-text tone note: "Warm and gentle, rainy evenings" -> ["Warm and gentle", "rainy evenings"]. */
export function toneChips(text, max = 6) {
  const seen = new Set();
  const out = [];
  for (const part of String(text ?? '').split(/[,;\n.]+/)) {
    const chip = part.trim().replace(/\s+/g, ' ');
    if (!chip || chip.length > 32 || seen.has(chip.toLowerCase())) continue;
    seen.add(chip.toLowerCase());
    out.push(chip);
    if (out.length >= max) break;
  }
  return out;
}

/** Keep going order: a decision waiting for you first, then stopped, then working or waiting, then the rest. */
const NEEDS_RANK = { todo: 0, bad: 1, run: 2, wait: 3, warn: 4, ok: 5 };

export function byNeedsYou(entries, toneOf) {
  return (entries ?? [])
    .map((entry, i) => ({ entry, i, rank: NEEDS_RANK[toneOf(entry) ?? ''] ?? 6 }))
    .sort((a, b) => a.rank - b.rank || a.i - b.i)
    .map(({ entry }) => entry);
}
