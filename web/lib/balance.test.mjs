import { test } from 'node:test';
import assert from 'node:assert/strict';
import { balanceDisplay, formatUsdMicros, parseBalanceInput } from './balance.mjs';

test('missing balance blocks live and says so', () => {
  for (const b of [null, undefined, { balance_usd_micros: null, fresh: false }]) {
    const d = balanceDisplay(b);
    assert.equal(d.tone, 'missing');
    assert.equal(d.liveBlocked, true);
    assert.match(d.text, /not recorded/);
  }
});

test('fresh balance shows the amount and age', () => {
  const d = balanceDisplay({ balance_usd_micros: 41_250_000, fresh: true, age_hours: 2.04, max_age_hours: 24, set_at: 'x' });
  assert.equal(d.tone, 'ok');
  assert.equal(d.liveBlocked, false);
  assert.equal(d.text, 'Higgsfield balance: $41.25, recorded 2.0 h ago');
});

test('stale balance is a warning and blocks live', () => {
  const d = balanceDisplay({ balance_usd_micros: 41_250_000, fresh: false, age_hours: 30, max_age_hours: 24, set_at: 'x' });
  assert.equal(d.tone, 'warn');
  assert.equal(d.liveBlocked, true);
  assert.match(d.text, /\$41\.25.*30\.0 h ago \(max 24 h\).*too old/);
});

test('usd micros format with integer rounding', () => {
  assert.equal(formatUsdMicros(1_520_000), '$1.52');
  assert.equal(formatUsdMicros(4_995_000), '$5.00');
  assert.equal(formatUsdMicros(-60_000), '-$0.06');
});

test('balance input accepts dollars with up to 2 decimals only', () => {
  assert.equal(parseBalanceInput(' 12.5 '), '12.5');
  assert.equal(parseBalanceInput('0'), '0');
  for (const bad of ['', '-1', '1.234', 'abc', '1e3', null]) assert.equal(parseBalanceInput(bad), null);
});
