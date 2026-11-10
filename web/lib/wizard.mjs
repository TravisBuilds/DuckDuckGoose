/**
 * Create-wizard helpers (web/app/studio/create/page.tsx): card facts, length choices inside a card's
 * range, the card's optional brief prompts, and the voices the creator picked, written into the
 * draft's audio plan (hfvg.audio_mix.normalize_plan schema: vo.voice_id speaks the narrated lines,
 * vo.speakers[name] each dialogue speaker's lines).
 */

/** Length window and default when the creator skips the cards (FREEFORM_PROFILE in hfvg/genre_presets.py). */
export const FREEFORM_LENGTH_RANGE = [30, 120];
export const FREEFORM_DEFAULT_LENGTH = 60;

/** Card groups on step 1, in display order (the 7 templates of hfvg/data/genre_presets.yaml). */
export const TEMPLATE_LABELS = {
  mood_loop: 'Mood loops',
  music_montage: 'Music montages',
  dialogue_episode: 'Dialogue episodes',
  continuous_pov: 'Continuous POV',
  cozy_micro: 'Cozy micro moments',
  action_sequence: 'Action',
  gag_short: 'Gag shorts',
};

const VOICE_LABELS = {
  none: 'no voice-over',
  caption_only: 'captions, no voice-over',
  narration: 'narrator voice',
  dialogue: 'a voice per speaker',
};

/** Length choices inside [lo, hi] (both ends and the default always included), ascending. */
export function lengthOptions(range, defaultLength) {
  const [lo, hi] = range;
  const span = hi - lo;
  const step = span <= 20 ? 5 : span <= 60 ? 15 : 30;
  const values = new Set([lo, hi, defaultLength]);
  for (let v = lo + step; v < hi; v += step) values.add(v);
  return Array.from(values).filter(v => v >= lo && v <= hi).sort((a, b) => a - b);
}

export function formatAspect(aspect) {
  return aspect === '16:9_rotated_to_9:16' ? '16:9 rotated to fill 9:16' : aspect;
}

export function soundLabel(preset) {
  return `${VOICE_LABELS[preset.voiceOver] ?? preset.voiceOver} · ${preset.music}`;
}

/** The cards grouped by template in TEMPLATE_LABELS order (unknown templates last); card order is kept. */
export function groupByTemplate(presets) {
  const order = Object.keys(TEMPLATE_LABELS);
  const rank = t => (order.includes(t) ? order.indexOf(t) : order.length);
  const groups = new Map();
  for (const preset of presets) {
    if (!groups.has(preset.template)) groups.set(preset.template, []);
    groups.get(preset.template).push(preset);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => rank(a) - rank(b))
    .map(([template, cards]) => ({ template, label: TEMPLATE_LABELS[template] ?? template, presets: cards }));
}

/** "cast_roles" -> "Cast roles", "character_a" -> "Character A". */
export function briefFieldLabel(field) {
  const text = String(field).replace(/_/g, ' ').trim().replace(/\b([a-z])\b/g, letter => letter.toUpperCase());
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** The idea plus one "Label: answer" line per answered brief field (blank answers are left out). */
export function composeIdea(idea, briefFields, answers) {
  const lines = (briefFields || [])
    .map(field => [field, String(answers?.[field] ?? '').trim()])
    .filter(([, answer]) => answer)
    .map(([field, answer]) => `${briefFieldLabel(field)}: ${answer}`);
  const base = String(idea ?? '').trim();
  return lines.length ? `${base}\n\n${lines.join('\n')}` : base;
}

/** A voice slot key as a test-id / DOM-safe token ("speaker:Old Captain" -> "speaker-old-captain"). */
export function slotToken(key) {
  return String(key).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
}

/** The draft's voice slots that have voice-over lines and no picked voice yet. */
export function missingVoiceSlots(slots, choices) {
  return (slots || []).filter(slot => slot.lines > 0 && !choices?.[slot.key]?.voice_id);
}

/**
 * Why Start is blocked, or null: a voiced slot has no voice and the server has no default voice
 * (ELEVENLABS_DEFAULT_VOICE_ID). The API refuses the same start; this says so before any upload.
 */
export function voiceBlockMessage(slots, choices, defaultVoiceAvailable) {
  const missing = missingVoiceSlots(slots, choices);
  if (!missing.length || defaultVoiceAvailable) return null;
  const names = missing.map(slot => slot.label).join(', ');
  return `Choose a voice before starting: ${names}. Describe each voice above and press "Find a voice".`;
}

/**
 * A copy of the draft's audio plan with the picked voices: the narrator slot sets vo.voice_id, each
 * speaker slot sets vo.speakers[speaker]. Slots without a pick and plans without voice-over stay as
 * they are.
 */
export function applyVoiceChoices(plan, slots, choices) {
  const out = JSON.parse(JSON.stringify(plan ?? {}));
  if (!out.vo) return out;
  for (const slot of slots || []) {
    const voiceId = choices?.[slot.key]?.voice_id;
    if (!voiceId) continue;
    if (slot.role === 'narrator') {
      out.vo.voice_id = voiceId;
    } else if (slot.role === 'speaker' && slot.speaker) {
      out.vo.speakers = { ...(out.vo.speakers || {}), [slot.speaker]: voiceId };
    }
  }
  return out;
}
