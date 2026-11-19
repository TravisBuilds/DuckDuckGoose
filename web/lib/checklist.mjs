/**
 * The story's reference list (hfvg/story_refs.py): the words each item and the list show. Pure
 * functions over what GET /api/worlds/{id} returns in `creation.checklist`, so the creation screen
 * and the tests agree.
 */

export const KIND_LABELS = { character: 'Character', place: 'Place', prop: 'Prop' };

/** The kind's chip colour: plain colours, so green, yellow and red keep meaning ready, needed and stopped. */
export const KIND_CHIPS = { character: 'pf-chip-lilac', place: 'pf-chip-blue', prop: 'pf-chip-open' };

/** The bar on an item's edge and its status words, by where it stands (lib/ui tones). */
export const STATUS_TONES = {
  ready: { edge: 'border-l-green', text: 'text-green-ink' },
  needs: { edge: 'border-l-mustard', text: 'text-mustard-ink' },
  optional: { edge: 'border-l-line', text: 'text-ink-soft' },
};

export const ADD_KINDS = [
  { kind: 'character', label: 'A character' },
  { kind: 'place', label: 'A place' },
  { kind: 'prop', label: 'A prop' },
];

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** "4 of 7 ready", and how many of the optional ones have photos. */
export function progressLine(progress) {
  const required = progress?.required ?? 0;
  const optional = progress?.optional ?? 0;
  if (required + optional === 0) return 'Nothing on the list yet';
  const main = required > 0 ? `${progress.ready ?? 0} of ${required} ready` : 'Everything on the list is optional';
  return optional > 0 ? `${main} · ${progress.optional_ready ?? 0} of ${optional} optional` : main;
}

/** How full the bar is, 0 to 100: the required items with enough photos. */
export function progressPercent(progress) {
  const required = progress?.required ?? 0;
  if (required === 0) return 0;
  return Math.round((100 * Math.min(progress.ready ?? 0, required)) / required);
}

/** Every required item has its photos. */
export function listReady(progress) {
  return (progress?.required ?? 0) > 0 && (progress.ready ?? 0) >= progress.required;
}

/** What the item wants: "Ideally 3 photos, at least 1". */
export function countsLine(item) {
  const ideal = item.ideal_photos;
  const least = Math.max(item.min_photos, 1);
  if (item.optional) return `Optional · ideally ${plural(ideal, 'photo')}`;
  return least >= ideal ? `${plural(ideal, 'photo')} needed` : `Ideally ${plural(ideal, 'photo')}, at least ${least}`;
}

/** Where the item stands: ready, ready with room for more, optional, or the photos it still needs. */
export function itemStatus(item) {
  const count = item.photo_count ?? item.photos?.length ?? 0;
  const least = Math.max(item.min_photos, 1);
  if (count >= item.ideal_photos) return { tone: 'ready', text: 'Ready' };
  if (count >= least) return { tone: 'ready', text: `Ready · ${plural(item.ideal_photos - count, 'more photo')} would help` };
  if (item.optional) return { tone: 'optional', text: count > 0 ? `${count} of ${item.ideal_photos} photos` : 'Optional' };
  return { tone: 'needs', text: `Needs ${plural(least - count, 'photo')}` };
}

/** The make button's words for where the story and its list are. */
export function makeListLabel(story) {
  if (!story?.list_made) return 'Make my reference list';
  return story.list_current ? 'Make the list again' : 'Update the list from my story';
}

/** Why the make button is off, or null when it is on. */
export function makeListBlocker({ saved, typed, storyMin }) {
  const least = storyMin ?? 20;
  if (typed.trim().length < least && saved.trim().length < least) {
    return `Turns on once your story has a few lines (at least ${least} letters).`;
  }
  return null;
}

/** The reference photos on an item, in its order (missing ones left out). */
export function itemPhotos(item, photos) {
  const byId = new Map((photos ?? []).map((p) => [p.image_id, p]));
  return (item?.photos ?? []).map((id) => byId.get(id)).filter(Boolean);
}

/** The reference photos on no item of the list: the ones added by hand before or beside it. */
export function otherPhotos(checklist, photos) {
  const listed = new Set((checklist?.items ?? []).flatMap((item) => item.photos ?? []));
  return (photos ?? []).filter((p) => !listed.has(p.image_id));
}

/** What making the list did, in a sentence. */
export function listMadeMessage(result) {
  const added = result?.added?.length ?? 0;
  const removed = result?.removed?.length ?? 0;
  const dropped = result?.dropped ?? [];
  const parts = [];
  if (added > 0) parts.push(`${plural(added, 'reference')} added`);
  if (removed > 0) parts.push(`${removed} the story no longer needs taken off`);
  let text = parts.length > 0 ? `Your list is ready: ${parts.join(', ')}.` : 'Your list already matches the story.';
  if (dropped.length > 0) text += ` Left out what the studio cannot make: ${dropped.join(', ')}.`;
  return text;
}
