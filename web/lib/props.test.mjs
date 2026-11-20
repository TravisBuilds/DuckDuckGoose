import { test } from 'node:test';
import assert from 'node:assert/strict';
import { emptyRights } from './cast.mjs';
import {
  MAX_PROPS_PER_SHOT,
  addPropProblem,
  castSlug,
  draftProblem,
  draftValue,
  historyText,
  historyValue,
  itemPhotos,
  planStateText,
  propAddForm,
  savedText,
  shotChanges,
  shotDraft,
  shotPlanChanges,
  toggleCastEntry,
  togglePick,
  withPhotoFields,
} from './props.mjs';

const MB = 1_048_576;
const photo = (extra = {}) => ({ name: 'cabin.png', type: 'image/png', size: 2 * MB, ...extra });
const original = { ...emptyRights(), rights: 'original' };

function shot(extra = {}) {
  return {
    shot_id: 'A01', prompt: 'A wide shot of a harbour at dawn', end_prompt: null, clip_prompt: null,
    end_frame: false, model: 'kling', duration: 5, cast: [], props: [], place: null, refs: [], notes: [],
    errors: [], history: [], ...extra,
  };
}

test('the add form starts from a photo and is only for whoever may change the library', () => {
  const limits = { max_items: 3 };
  assert.deepEqual(propAddForm({ can_edit: true, items: [], limits }),
    { shown: true, first: true, title: 'Add your first prop or place' });
  assert.deepEqual(propAddForm({ can_edit: true, items: [{}], limits }),
    { shown: true, first: false, title: 'Add a prop or place' });
  assert.equal(propAddForm({ can_edit: false, items: [], limits }).shown, false);
  assert.equal(propAddForm({ can_edit: true, items: [{}, {}, {}], limits }).shown, false);
  assert.equal(propAddForm(null).shown, false);
});

test('a prop or place with a photo needs a kind, a photo that passes the checks, a name and the rights answer', () => {
  assert.equal(addPropProblem('', 'Cabin', photo(), original, 10 * MB), 'Choose whether this is a prop or a place.');
  assert.equal(addPropProblem('person', 'Cabin', photo(), original, 10 * MB), 'Choose whether this is a prop or a place.');
  assert.equal(addPropProblem('place', 'Cabin', null, original, 10 * MB), 'Choose a photo first.');
  assert.equal(addPropProblem('place', 'Cabin', photo({ type: 'image/gif', name: 'a.gif' }), original, 10 * MB),
    'Pictures must be PNG, JPG or WebP files.');
  assert.match(addPropProblem('place', 'Cabin', photo({ size: 11 * MB }), original, 10 * MB), /can be at most 10/);
  assert.equal(addPropProblem('place', '  ', photo(), original, 10 * MB), 'Give the place a name.');
  assert.match(addPropProblem('prop', 'Umbrella', photo(), emptyRights(), 10 * MB), /^Tell us whose picture this is/);
  assert.match(addPropProblem('prop', 'Umbrella', photo(), { ...original, realPerson: true }, 10 * MB),
    /needs their permission/);
  assert.equal(addPropProblem('prop', 'Umbrella', photo(), { ...original, realPerson: true, likenessConsent: true },
    10 * MB), null);
  assert.equal(addPropProblem('prop', 'Umbrella', photo(), original, 10 * MB), null);
});

test('the with-photo form sends the kind, trimmed name and description and the rights answers', () => {
  assert.deepEqual(withPhotoFields({ kind: 'place', name: ' Harbour cabin ', description: ' a blue wooden cabin ' },
    original), [
    ['kind', 'place'], ['name', 'Harbour cabin'], ['description', 'a blue wooden cabin'],
    ['rights', 'original'], ['depicts_real_person', 'false'], ['likeness_consent', 'false'],
  ]);
});

test('an item shows its main photo first', () => {
  const photos = [
    { image_id: 'a', status: 'active', is_primary: false },
    { image_id: 'b', status: 'active', is_primary: true },
    { image_id: 'c', status: 'removed', is_primary: false },
  ];
  assert.deepEqual(itemPhotos({ photos }).map(p => p.image_id), ['b', 'a']);
  assert.deepEqual(itemPhotos({}), []);
});

test('picks toggle in order, up to the limit; a character keeps its outfit pick until removed', () => {
  assert.equal(MAX_PROPS_PER_SHOT, 4);
  assert.deepEqual(togglePick([], 'umbrella', 4), ['umbrella']);
  assert.deepEqual(togglePick(['umbrella', 'kite'], 'umbrella', 4), ['kite']);
  assert.deepEqual(togglePick(['a', 'b', 'c', 'd'], 'e', 4), ['a', 'b', 'c', 'd']);
  const cast = [{ character: 'mara', version: 2 }, 'pip'];
  assert.equal(castSlug(cast[0]), 'mara');
  assert.equal(castSlug(cast[1]), 'pip');
  assert.deepEqual(toggleCastEntry(cast, 'tom', 6), [...cast, 'tom']);
  assert.deepEqual(toggleCastEntry(cast, 'mara', 6), ['pip']);
  assert.deepEqual(toggleCastEntry(cast, 'tom', 2), cast);
});

test('a save sends only what changed, per shot, with the note', () => {
  const shots = [shot(), shot({ shot_id: 'A02', cast: [{ character: 'mara', version: 2 }], place: 'cabin' })];
  assert.equal(shotPlanChanges(shots, {}), null);
  assert.equal(shotPlanChanges(shots, { A01: shotDraft(shots[0]), A02: shotDraft(shots[1]) }), null);
  assert.deepEqual(shotChanges(shots[1], shotDraft(shots[1])), {});
  // only the touched fields are sent: the outfit pick of A02 stays as it is
  const drafts = {
    A01: { prompt: '  A wide shot of a harbour at dusk  ', place: 'cabin' },
    A02: { props: ['umbrella'] },
  };
  assert.deepEqual(shotPlanChanges(shots, drafts, ' warmer light '), {
    shots: {
      A01: { prompt: 'A wide shot of a harbour at dusk', place: 'cabin' },
      A02: { props: ['umbrella'] },
    },
    note: 'warmer light',
  });
  // clearing a place or the cast sends an empty value; clearing an empty motion prompt is no change
  assert.deepEqual(shotPlanChanges(shots, { A02: { place: '', cast: [], clip_prompt: '' } }),
    { shots: { A02: { place: '', cast: [] } } });
  // white space around a prompt is not a change; a value put back is not a change
  assert.equal(shotPlanChanges(shots, { A01: { prompt: ' A wide shot of a harbour at dawn ' } }), null);
  assert.equal(shotPlanChanges(shots, { A02: { place: 'cabin', cast: [{ character: 'mara', version: 2 }] } }), null);
});

test('the editor shows the unsaved edit of a field, or what is saved', () => {
  const saved = shot({ place: 'cabin', props: ['umbrella'] });
  assert.equal(draftValue(saved, undefined, 'place'), 'cabin');
  assert.equal(draftValue(saved, { place: '' }, 'place'), '');
  assert.deepEqual(draftValue(saved, { place: '' }, 'props'), ['umbrella']);
  assert.equal(draftValue(saved, {}, 'clip_prompt'), '');
});

test('a shot without a picture prompt cannot be saved', () => {
  const shots = [shot()];
  assert.equal(draftProblem(shots, { A01: { prompt: '   ' } }), 'Shot A01 needs a picture prompt.');
  assert.equal(draftProblem(shots, { A01: { place: 'cabin' } }), null);
  assert.equal(draftProblem(shots, { A01: shotDraft(shots[0]) }), null);
  assert.equal(draftProblem(shots, {}), null);
});

test('the history says who changed what, in words', () => {
  assert.equal(historyText({ field: 'prompt', old: 'a', new: 'b', actor_name: 'Mara' }), 'Mara changed the picture prompt');
  assert.equal(historyText({ field: 'place', old: null, new: 'cabin', actor_name: 'Admin' }), 'Admin set the place');
  assert.equal(historyText({ field: 'props', old: ['umbrella'], new: [], actor_name: null }), 'Someone removed the props');
  assert.equal(historyText({ field: 'clip_prompt', old: null, new: 'pans left' }), 'Someone set the motion prompt');
  const names = { cabin: 'Harbour cabin', umbrella: 'Red umbrella', mara: 'Mara' };
  assert.equal(historyValue('props', ['umbrella', 'kite'], names), 'Red umbrella, kite');
  assert.equal(historyValue('place', 'cabin', names), 'Harbour cabin');
  assert.equal(historyValue('cast', [{ character: 'mara', version: 2 }], names), 'Mara');
  assert.equal(historyValue('prompt', 'A wide shot', names), 'A wide shot');
  assert.equal(historyValue('place', null, names), 'nothing');
  assert.equal(historyValue('props', [], names), 'nothing');
});

test('the plan says when it can change and who may change it', () => {
  assert.match(planStateText({ uploaded: false }), /Upload this video’s shot list first/);
  assert.equal(planStateText({ uploaded: true, may_change: true, phase: { state: 'locked', reason: 'Approved.' } }),
    'Approved.');
  assert.equal(planStateText({ uploaded: true, may_change: false, phase: { state: 'g1' } }),
    'Only this video’s owner (or an admin) can change the shot plan.');
  assert.match(planStateText({ uploaded: true, may_change: true, phase: { state: 'g1' } }), /checked again/);
  assert.equal(planStateText({ uploaded: true, may_change: true, phase: { state: 'open' } }),
    'Saved changes are used when this video starts.');
});

test('the result line of a save: members are told which shots to change, admins get the checks\' own words', () => {
  assert.equal(savedText({ changed: [] }), null);
  assert.deepEqual(savedText({ changed: [{ shot_id: 'A01', field: 'prompt' }, { shot_id: 'A01', field: 'place' }],
    result: null }), { tone: 'ok', text: 'Saved 1 shot.' });
  assert.deepEqual(savedText({ changed: [{ shot_id: 'A01', field: 'prompt' }, { shot_id: 'A02', field: 'place' }],
    result: { preflight_errors: [] } }),
  { tone: 'ok', text: 'Saved 2 shots and checked the plan again. Approve it when you are ready.' });
  const broken = { changed: [{ shot_id: 'A01', field: 'clip_prompt' }],
    result: { preflight_errors: ['shot A01: kling prompt is 2600 characters (max 2500)'] } };
  assert.deepEqual(savedText(broken, { detail: true }),
    { tone: 'problem', text: 'Saved 1 shot, but a shot asks for more than the studio can make: shot A01: kling prompt is 2600 characters (max 2500)' });
  assert.deepEqual(savedText(broken), { tone: 'problem',
    text: 'Saved 1 shot, but shot A01 asks for more than the studio can make. Try a shorter prompt or fewer picks, or ask an admin what to change.' });
  const both = { changed: [{ shot_id: 'S01', field: 'prompt' }, { shot_id: 'S02', field: 'cast' }], result: { preflight_errors: [
    'Shot S01 picture prompt is 3100 characters; seedream-v4 accepts 3000. Limit source: docs.example.test, checked 2026-01-05.',
    'Shot S02 (start frame) uses 6 reference images, but kling-v2.1 accepts 4.',
    'Shot S02 clip: kling-v2.1 does not support end frames.',
  ] } };
  assert.equal(savedText(both)?.text,
    'Saved 2 shots, but shots S01 and S02 ask for more than the studio can make. Try a shorter prompt or fewer picks, or ask an admin what to change.');
  assert.equal(savedText({ changed: [{ shot_id: 'S01', field: 'prompt' }], result: { preflight_errors: ['The plan breaks a limit'] } })?.text,
    'Saved 1 shot, but a shot asks for more than the studio can make. Try a shorter prompt or fewer picks, or ask an admin what to change.');
  for (const response of [broken, both]) {
    assert.doesNotMatch(savedText(response)?.text ?? '', /kling|seedream|Limit source|model/i);
  }
});
