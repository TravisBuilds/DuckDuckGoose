/**
 * Props and places helpers for the Props & places panel (web/components/PropsPanel.tsx) and the shot
 * plan editor (web/components/ShotPlanEditor.tsx): what a prop or place with a photo needs and sends
 * (the photo gets the cast picture checks and rights questions of lib/cast.mjs; the API checks it
 * again: api/prop_routes.py), each shot's picks and prompt edits, what a save sends
 * (api/shot_plan_routes.py) and how the edit history and the plan's state read.
 */
import { pictureProblem, rightsFields, rightsProblem } from './cast.mjs';

/** At most this many props in one shot, besides its place (hfvg/cast_prompt.py MAX_PROPS_PER_SHOT). */
export const MAX_PROPS_PER_SHOT = 4;
export const KINDS = ['prop', 'place'];
export const KIND_TEXT = {
  prop: { label: 'Prop', choice: 'A prop', hint: 'a thing in the shot, like a red umbrella or a bicycle' },
  place: { label: 'Place', choice: 'A place', hint: 'where the shot happens, like a harbour cabin or a kitchen' },
};

/**
 * The panel's add form: for whoever may change the library (the API's can_edit: the episode's owner
 * or an admin, and the series' owner for a series library) while there is room. It starts from a photo.
 */
export function propAddForm(view) {
  const count = view?.items?.length ?? 0;
  return {
    shown: Boolean(view?.can_edit) && count < (view?.limits?.max_items ?? 0),
    first: count === 0,
    title: count === 0 ? 'Add your first prop or place' : 'Add a prop or place',
  };
}

/** Why "Add with this photo" cannot be pressed yet (null when it can). */
export function addPropProblem(kind, name, file, answers, maxBytes) {
  if (!KINDS.includes(kind)) return 'Choose whether this is a prop or a place.';
  if (!file) return 'Choose a photo first.';
  const picture = pictureProblem(file, maxBytes);
  if (picture) return picture;
  if (!String(name ?? '').trim()) return `Give the ${kind} a name.`;
  return rightsProblem(answers);
}

/** The form fields, besides the photo, of POST /api/episodes/{id}/props/with-photo. */
export function withPhotoFields(fields, answers) {
  return [
    ['kind', String(fields?.kind ?? '')],
    ['name', String(fields?.name ?? '').trim()],
    ['description', String(fields?.description ?? '').trim()],
    ...rightsFields(answers),
  ];
}

/** An item's photos, main photo first. */
export function itemPhotos(item) {
  return (item?.photos ?? []).filter(p => p.status === 'active')
    .sort((a, b) => Number(b.is_primary) - Number(a.is_primary));
}

// ---------------------------------------------------------------------------------- the shot plan

export const FIELD_TEXT = {
  prompt: 'the picture prompt',
  end_prompt: 'the end picture prompt',
  clip_prompt: 'the motion prompt',
  cast: 'who is in the shot',
  props: 'the props',
  place: 'the place',
};
const TEXT_FIELDS = ['prompt', 'end_prompt', 'clip_prompt'];

/** The character id of a shots.json cast entry ("mara" or {character: "mara", version: 2}). */
export function castSlug(entry) {
  return typeof entry === 'string' ? entry : entry?.character;
}

/** A shot's editable values as the editor keeps them: text for the prompts, lists for the picks. */
export function shotDraft(shot) {
  return {
    prompt: shot?.prompt ?? '',
    end_prompt: shot?.end_prompt ?? '',
    clip_prompt: shot?.clip_prompt ?? '',
    cast: [...(shot?.cast ?? [])],
    props: [...(shot?.props ?? [])],
    place: shot?.place ?? '',
  };
}

/** Add or remove an id (order kept; at most `max`). */
export function togglePick(list, slug, max) {
  if (list.includes(slug)) return list.filter(s => s !== slug);
  if (max && list.length >= max) return list;
  return [...list, slug];
}

/** Add or remove a character; a removed one loses its outfit pick, an added one gets its usual look. */
export function toggleCastEntry(entries, slug, max) {
  if (entries.some(e => castSlug(e) === slug)) return entries.filter(e => castSlug(e) !== slug);
  if (max && entries.length >= max) return entries;
  return [...entries, slug];
}

function sameText(a, b) {
  return String(a ?? '').trim() === String(b ?? '').trim();
}

function sameList(a, b) {
  return JSON.stringify(a ?? []) === JSON.stringify(b ?? []);
}

/**
 * What the editor shows for a field: the unsaved edit, or what is saved. A draft holds only the
 * fields the creator touched, so a save never sends back (and undoes) a change made elsewhere since.
 */
export function draftValue(shot, draft, field) {
  return draft && field in draft ? draft[field] : shotDraft(shot)[field];
}

/** The touched fields of a shot whose draft differs from what is saved ({} when none). */
export function shotChanges(shot, draft) {
  const saved = shotDraft(shot);
  const out = {};
  for (const [field, value] of Object.entries(draft ?? {})) {
    if (TEXT_FIELDS.includes(field)) {
      if (!sameText(saved[field], value)) out[field] = String(value ?? '').trim();
    } else if (field === 'cast' || field === 'props') {
      if (!sameList(saved[field], value)) out[field] = [...(value ?? [])];
    } else if (field === 'place' && (saved.place || '') !== (value || '')) {
      out.place = value || '';
    }
  }
  return out;
}

/** POST /api/episodes/{id}/shot-plan body for the shots whose drafts changed; null when none did. */
export function shotPlanChanges(shots, drafts, note = '') {
  const body = { shots: {} };
  for (const shot of shots ?? []) {
    const changes = shotChanges(shot, drafts?.[shot.shot_id]);
    if (Object.keys(changes).length) body.shots[shot.shot_id] = changes;
  }
  if (!Object.keys(body.shots).length) return null;
  const why = String(note ?? '').trim();
  return why ? { ...body, note: why } : body;
}

/** Why the changes cannot be saved yet (null when they can): a shot without a picture prompt. */
export function draftProblem(shots, drafts) {
  for (const shot of shots ?? []) {
    const draft = drafts?.[shot.shot_id];
    if (draft && 'prompt' in draft && !String(draft.prompt ?? '').trim()) return `Shot ${shot.shot_id} needs a picture prompt.`;
  }
  return null;
}

function isEmpty(value) {
  return value == null || value === '' || (Array.isArray(value) && value.length === 0);
}

/** "Mara changed the picture prompt" / "set the place" / "removed the props". */
export function historyText(entry) {
  const who = entry?.actor_name || 'Someone';
  const what = FIELD_TEXT[entry?.field] ?? entry?.field;
  const verb = isEmpty(entry?.new) ? 'removed' : isEmpty(entry?.old) ? 'set' : 'changed';
  return `${who} ${verb} ${what}`;
}

/** A history value in words: the prompt itself, or the picked names ("Harbour cabin, Red umbrella"). */
export function historyValue(field, value, names = {}) {
  if (isEmpty(value)) return 'nothing';
  if (TEXT_FIELDS.includes(field)) return String(value);
  const list = Array.isArray(value) ? value : [value];
  return list.map(v => names[castSlug(v)] ?? castSlug(v)).join(', ');
}

/** What the editor says about when the plan can change (the API's phase: open, g1 or locked). */
export function planStateText(view) {
  if (!view?.uploaded) return 'Upload this video’s shot list first; then pick what is in each shot here.';
  const state = view.phase?.state;
  if (state === 'locked') return view.phase?.reason || 'The shot plan cannot change right now.';
  if (!view.may_change) return 'Only this video’s owner (or an admin) can change the shot plan.';
  if (state === 'g1') return 'Saved changes are checked again (model limits and prices) before you approve the plan.';
  return 'Saved changes are used when this video starts.';
}

/** The result line after a save (null for a save that changed nothing). */
export function savedText(response) {
  if (!response?.changed?.length) return null;
  const shots = new Set(response.changed.map(c => c.shot_id)).size;
  const what = `${shots} shot${shots === 1 ? '' : 's'}`;
  const errors = response.result?.preflight_errors ?? [];
  if (errors.length) {
    return { tone: 'problem', text: `Saved ${what}, but the plan breaks a model limit: ${errors.join(' ')}` };
  }
  if (response.result) return { tone: 'ok', text: `Saved ${what} and checked the plan again. Approve it when you are ready.` };
  return { tone: 'ok', text: `Saved ${what}.` };
}
