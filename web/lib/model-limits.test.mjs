// T23: run with `node --test web/lib/` (or `npm run test:unit` in web/). No dependencies.
import test from 'node:test';
import assert from 'node:assert/strict';
import { formatLimits, formatLimitValue, summarizePreflight } from './model-limits.mjs';

const grok = {
  model_id: 'xai/grok-imagine-image-2.0', kind: 'image', status: 'active_default_still',
  limits: {
    max_refs: { value: 5, sources: [{ url: 'https://docs.x.ai/docs/guides/image-generations', checked: '2026-10-19' }] },
    prompt_max_chars: { value: 'not published', note: 'No documented limit' },
    aspect_ratios: { value: ['auto', '9:16', '16:9'], sources: [] },
    quality: { value: ['low', 'medium'] },
    outputs_per_request: { value: 1 },
  },
};
const kling = {
  model_id: 'kling-video/v3.0/pro/image-to-video', kind: 'video', status: 'active_default_clip',
  limits: {
    duration_s: { value: { min: 3, max: 15, integer: true } },
    prompt_max_chars: { value: 2500 },
    frame_formats: { value: ['image/jpeg', 'image/png'] },
    frame_max_bytes: { value: 10485760 },
    max_refs: { value: 0 },
  },
};

test('formatLimits shows creator labels in order, with the source host and date', () => {
  const rows = formatLimits(grok);
  assert.deepEqual(rows.map((r) => r.label), ['Reference images', 'Prompt length', 'Aspect ratios', 'Quality']);
  assert.equal(rows[0].value, 'up to 5');
  assert.equal(rows[0].source, 'docs.x.ai, checked 2026-10-19');
  assert.equal(rows[1].value, 'not published');
  assert.equal(rows[1].note, 'No documented limit');
  assert.equal(rows[2].value, 'auto, 9:16, 16:9');
  assert.equal(rows[2].source, null);
});

test('clip limits: durations, prompt cap, frame formats and size', () => {
  const byLabel = Object.fromEntries(formatLimits(kling).map((r) => [r.label, r.value]));
  assert.equal(byLabel['Duration'], '3–15 s (whole seconds)');
  assert.equal(byLabel['Prompt length'], '2,500 characters');
  assert.equal(byLabel['Frame formats'], 'JPEG, PNG');
  assert.equal(byLabel['Frame file size'], 'up to 10 MB');
  assert.equal(byLabel['Reference images'], 'none');
  assert.equal(formatLimitValue('duration_ms', { min: 3000, max: 300000 }), '3–300 s');
  assert.equal(formatLimitValue('max_refs', null), 'not published');
});

test('summarizePreflight counts per-shot pass/fail', () => {
  const s = summarizePreflight({
    A02: { ok: false, errors: ['x'], warnings: [] },
    A01: { ok: true, errors: [], warnings: ['w'] },
    A03: { ok: false, errors: ['y'], warnings: [] },
  });
  assert.deepEqual(s, { passed: 1, failed: 2, failedIds: ['A02', 'A03'] });
  assert.deepEqual(summarizePreflight({}), { passed: 0, failed: 0, failedIds: [] });
});
