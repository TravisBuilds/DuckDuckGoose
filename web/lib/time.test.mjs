import test from 'node:test';
import assert from 'node:assert/strict';

import { importTs } from './ts-module.mjs';

const { parseApiDate, formatLocalDate, formatLocalDateTime } = await importTs(new URL('./time.ts', import.meta.url));

test('a timestamp without a zone is UTC (SQLite CURRENT_TIMESTAMP and naive isoformat)', () => {
  assert.equal(parseApiDate('2026-11-15 07:56:56').toISOString(), '2026-11-15T07:56:56.000Z');
  assert.equal(parseApiDate('2026-11-15T07:56:56.123456').toISOString(), '2026-11-15T07:56:56.123Z');
});

test('a timestamp with a zone keeps it', () => {
  assert.equal(parseApiDate('2026-11-15T07:56:56Z').toISOString(), '2026-11-15T07:56:56.000Z');
  assert.equal(parseApiDate('2026-11-15T07:56:56+02:00').toISOString(), '2026-11-15T05:56:56.000Z');
  assert.equal(parseApiDate('2026-11-15T07:56:56-0500').toISOString(), '2026-11-15T12:56:56.000Z');
});

test('missing or unreadable values give no date', () => {
  for (const value of [null, undefined, '', '   ', 'yesterday', '2026-13-45 99:00:00']) {
    assert.equal(parseApiDate(value), null, String(value));
    assert.equal(formatLocalDate(value), '');
    assert.equal(formatLocalDateTime(value), '');
  }
});

test('the date shown is the viewer\'s day, not the UTC day (the old off-by-one)', () => {
  // 20:00 UTC is already the next morning in Tokyo, and 02:00 UTC is still the previous evening in LA
  assert.equal(formatLocalDate('2026-11-15 20:00:00', 'en-US', 'Asia/Tokyo'), 'Nov 16, 2026');
  assert.equal(formatLocalDate('2026-11-15 02:00:00', 'en-US', 'America/Los_Angeles'), 'Nov 14, 2026');
  assert.equal(formatLocalDate('2026-11-15 12:00:00', 'en-US', 'UTC'), 'Nov 15, 2026');
  // some ICU versions put a narrow no-break space before AM
  assert.equal(formatLocalDateTime('2026-11-15 20:05:00', 'en-US', 'Asia/Tokyo').replace(/\s/g, ' '), 'Nov 16, 2026, 5:05 AM');
});

test('a calendar day stays the same day in every zone', () => {
  const day = parseApiDate('2026-11-15');
  assert.deepEqual([day.getFullYear(), day.getMonth(), day.getDate()], [2026, 10, 15]);
  assert.equal(formatLocalDate('2026-11-15', 'en-US'), 'Nov 15, 2026');
});
