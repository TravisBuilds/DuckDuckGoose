// Creator-facing formatting of model limits (GET /api/models) and preflight results
// (POST /api/episodes/{id}/preflight). Plain ESM so `node --test` runs it without a build step.

/** @typedef {{url: string, checked: string}} LimitSource */
/** @typedef {{value: unknown, note?: string | null, basis?: string, sources?: LimitSource[]}} Limit */
/** @typedef {{model_id: string, kind: string, status: string, limits: Record<string, Limit>}} ModelView */
/** @typedef {{label: string, value: string, source: string | null, note: string | null}} LimitRow */

/** Limits a creator needs when picking a model, in display order. */
export const LIMIT_LABELS = {
  max_refs: 'Reference images',
  prompt_max_chars: 'Prompt length',
  aspect_ratios: 'Aspect ratios',
  resolutions: 'Resolutions',
  quality: 'Quality',
  duration_s: 'Duration',
  start_frame: 'Start frame',
  end_frame: 'End frame',
  frame_formats: 'Frame formats',
  frame_max_bytes: 'Frame file size',
  frame_min_px: 'Frame min side',
  text_max_chars: 'Text length',
  duration_ms: 'Music length',
};

const MIME = { 'image/jpeg': 'JPEG', 'image/png': 'PNG', 'image/webp': 'WebP', 'image/gif': 'GIF' };

function mb(bytes) {
  return `${Math.floor((bytes + 1) / (1024 * 1024))} MB`;
}

/**
 * One limit value as text. Unknown values stay "not published" (never guessed).
 * @param {string} name
 * @param {unknown} value
 * @returns {string}
 */
export function formatLimitValue(name, value) {
  if (value === 'not published' || value === null || value === undefined) return 'not published';
  if (name === 'max_refs' && typeof value === 'number') return value === 0 ? 'none' : `up to ${value}`;
  if ((name === 'prompt_max_chars' || name === 'text_max_chars') && typeof value === 'number') {
    return `${value.toLocaleString('en-US')} characters`;
  }
  if (name === 'frame_max_bytes' && typeof value === 'number') return `up to ${mb(value)}`;
  if (name === 'frame_min_px' && typeof value === 'number') return `${value} px`;
  if (name === 'duration_s' && typeof value === 'object' && !Array.isArray(value)) {
    const d = /** @type {{min: number, max: number, integer?: boolean}} */ (value);
    return `${d.min}–${d.max} s${d.integer ? ' (whole seconds)' : ''}`;
  }
  if (name === 'duration_ms' && typeof value === 'object' && !Array.isArray(value)) {
    const d = /** @type {{min: number, max: number}} */ (value);
    return `${d.min / 1000}–${d.max / 1000} s`;
  }
  if (Array.isArray(value)) {
    return value.map((v) => (typeof v === 'string' && MIME[v]) || String(v)).join(', ');
  }
  return String(value);
}

/**
 * Rows for the limits card: known labels only, in LIMIT_LABELS order, with the first source host and date.
 * @param {ModelView} model
 * @returns {LimitRow[]}
 */
export function formatLimits(model) {
  const rows = [];
  for (const [name, label] of Object.entries(LIMIT_LABELS)) {
    const limit = model.limits?.[name];
    if (!limit) continue;
    const src = limit.sources && limit.sources[0];
    let source = null;
    if (src && src.url) {
      const host = src.url.startsWith('https://') ? src.url.slice(8).split('/')[0] : src.url;
      source = `${host}, checked ${src.checked}`;
    }
    rows.push({ label, value: formatLimitValue(name, limit.value), source, note: limit.note ?? null });
  }
  return rows;
}

/**
 * Per-shot pass/fail summary from the preflight endpoint's `shots` map.
 * @param {Record<string, {ok: boolean, errors: string[], warnings: string[]}>} shots
 * @returns {{passed: number, failed: number, failedIds: string[]}}
 */
export function summarizePreflight(shots) {
  const ids = Object.keys(shots || {}).sort();
  const failedIds = ids.filter((id) => !shots[id].ok);
  return { passed: ids.length - failedIds.length, failed: failedIds.length, failedIds };
}
