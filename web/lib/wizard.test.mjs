import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  FREEFORM_DEFAULT_LENGTH,
  FREEFORM_LENGTH_RANGE,
  TEMPLATE_LABELS,
  applyVoiceChoices,
  briefFieldLabel,
  composeIdea,
  formatAspect,
  groupByTemplate,
  lengthOptions,
  missingVoiceSlots,
  slotToken,
  soundLabel,
  voiceBlockMessage,
} from './wizard.mjs';

const NARRATOR = { key: 'narrator', role: 'narrator', label: 'Narrator', speaker: null, lines: 3, suggestion: 'calm' };
const LEAD = { key: 'speaker:Lead', role: 'speaker', label: 'Lead', speaker: 'Lead', lines: 2, suggestion: 'bright' };
const FRIEND = { key: 'speaker:Friend', role: 'speaker', label: 'Friend', speaker: 'Friend', lines: 2, suggestion: 'relaxed' };

test('length options stay inside the range and always offer both ends and the default', () => {
  assert.deepEqual(lengthOptions(FREEFORM_LENGTH_RANGE, FREEFORM_DEFAULT_LENGTH), [30, 60, 90, 120]);
  assert.deepEqual(lengthOptions([10, 15], 12), [10, 12, 15]);
  assert.deepEqual(lengthOptions([25, 40], 30), [25, 30, 35, 40]);
  assert.deepEqual(lengthOptions([60, 90], 75), [60, 75, 90]);
  assert.deepEqual(lengthOptions([60, 170], 120), [60, 90, 120, 150, 170]);
  for (let lo = 10; lo <= 150; lo += 7) {
    for (const span of [0, 5, 13, 20, 45, 60, 100]) {
      const hi = Math.min(180, lo + span);
      const def = Math.round((lo + hi) / 2);
      const options = lengthOptions([lo, hi], def);
      assert.ok(options.includes(lo) && options.includes(hi) && options.includes(def), `${lo}-${hi}`);
      assert.ok(options.every(v => v >= lo && v <= hi), `${lo}-${hi}`);
      assert.deepEqual(options, [...options].sort((a, b) => a - b));
      assert.ok(options.length <= 8, `${lo}-${hi}: ${options}`);
    }
  }
});

test('skip uses the freeform 30-120 s window with 60 s preselected', () => {
  assert.deepEqual(FREEFORM_LENGTH_RANGE, [30, 120]);
  assert.equal(FREEFORM_DEFAULT_LENGTH, 60);
});

test('card facts: aspect and sound labels', () => {
  assert.equal(formatAspect('16:9_rotated_to_9:16'), '16:9 rotated to fill 9:16');
  assert.equal(formatAspect('4:3'), '4:3');
  assert.equal(soundLabel({ voiceOver: 'dialogue', music: 'light score' }), 'a voice per speaker · light score');
  assert.equal(soundLabel({ voiceOver: 'none', music: 'swelling score' }), 'no voice-over · swelling score');
  assert.equal(soundLabel({ voiceOver: 'caption_only', music: 'pop' }), 'captions, no voice-over · pop');
  assert.equal(soundLabel({ voiceOver: 'narration', music: 'drums' }), 'narrator voice · drums');
});

test('cards group by template in display order and keep their order inside a group', () => {
  const cards = [
    { id: 'a', template: 'gag_short' }, { id: 'b', template: 'mood_loop' }, { id: 'c', template: 'gag_short' },
    { id: 'd', template: 'something_new' }, { id: 'e', template: 'dialogue_episode' },
  ];
  const groups = groupByTemplate(cards);
  assert.deepEqual(groups.map(g => g.template), ['mood_loop', 'dialogue_episode', 'gag_short', 'something_new']);
  assert.deepEqual(groups.map(g => g.label), ['Mood loops', 'Dialogue episodes', 'Gag shorts', 'something_new']);
  assert.deepEqual(groups[2].presets.map(p => p.id), ['a', 'c']);
  assert.equal(Object.keys(TEMPLATE_LABELS).length, 7);
});

test('brief answers are appended to the idea, blanks left out', () => {
  assert.equal(briefFieldLabel('cast_roles'), 'Cast roles');
  assert.equal(briefFieldLabel('character_b'), 'Character B');
  assert.equal(briefFieldLabel('vessel_or_place'), 'Vessel or place');
  assert.equal(composeIdea('  A kite over the harbor ', ['era', 'place'], {}), 'A kite over the harbor');
  assert.equal(
    composeIdea('A kite over the harbor', ['era', 'place', 'cast_roles'], { era: ' 1990s ', place: '', cast_roles: 'two kids', other: 'x' }),
    'A kite over the harbor\n\nEra: 1990s\nCast roles: two kids',
  );
});

test('slot tokens are safe test ids', () => {
  assert.equal(slotToken('narrator'), 'narrator');
  assert.equal(slotToken('speaker:Old Captain'), 'speaker-old-captain');
});

test('start is blocked until every voiced slot has a voice, unless the server has a default voice', () => {
  const slots = [NARRATOR, LEAD, { ...FRIEND, lines: 0 }];
  assert.deepEqual(missingVoiceSlots(slots, {}).map(s => s.key), ['narrator', 'speaker:Lead']);
  assert.equal(voiceBlockMessage(slots, {}, false),
    'Choose a voice before starting: Narrator, Lead. Describe each voice above and press "Find a voice".');
  assert.equal(voiceBlockMessage(slots, {}, true), null);
  const chosen = { narrator: { voice_id: 'v1' }, 'speaker:Lead': { voice_id: 'v2' } };
  assert.deepEqual(missingVoiceSlots(slots, chosen), []);
  assert.equal(voiceBlockMessage(slots, chosen, false), null);
  assert.equal(voiceBlockMessage([], {}, false), null);
});

test('narration: the picked voice becomes vo.voice_id', () => {
  const plan = { vo: { voice_id: null, model: 'eleven_v3', lines: [{ id: 'L01', at: 0.3, text: 'Hi' }] }, music: { prompt: 'x' } };
  const out = applyVoiceChoices(plan, [NARRATOR], { narrator: { voice_id: 'voice-n', name: 'N' } });
  assert.equal(out.vo.voice_id, 'voice-n');
  assert.deepEqual(out.vo.lines, plan.vo.lines);
  assert.equal(plan.vo.voice_id, null, 'the draft plan is not mutated');
});

test('dialogue: each speaker gets its own voice id in vo.speakers', () => {
  const plan = {
    vo: {
      voice_id: null, model: 'eleven_v3', speakers: { Lead: '', Friend: '' },
      lines: [{ id: 'L01', speaker: 'Lead', text: 'Hi' }, { id: 'L02', speaker: 'Friend', text: 'Hey' }],
    },
  };
  const out = applyVoiceChoices(plan, [LEAD, FRIEND], { 'speaker:Lead': { voice_id: 'v-lead' }, 'speaker:Friend': { voice_id: 'v-friend' } });
  assert.deepEqual(out.vo.speakers, { Lead: 'v-lead', Friend: 'v-friend' });
  assert.equal(out.vo.voice_id, null);
  const partial = applyVoiceChoices(plan, [LEAD, FRIEND], { 'speaker:Lead': { voice_id: 'v-lead' }, 'speaker:Friend': null });
  assert.deepEqual(partial.vo.speakers, { Lead: 'v-lead', Friend: '' });
  assert.deepEqual(plan.vo.speakers, { Lead: '', Friend: '' }, 'the draft plan is not mutated');
});

test('plans without voice-over come back unchanged', () => {
  const plan = { music: { prompt: 'calm piano', loop: true, level_db: -18 } };
  assert.deepEqual(applyVoiceChoices(plan, [], {}), plan);
  assert.deepEqual(applyVoiceChoices(null, [NARRATOR], { narrator: { voice_id: 'v' } }), {});
});
