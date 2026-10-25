// G-AUTH-1: run with `npm run test:unit` (node --test lib/). The fixed vectors are also asserted on
// the Python side (tests/test_session_tokens.py), so the API and the middleware agree on the format.
import test from 'node:test';
import assert from 'node:assert/strict';
import { sessionKeyFromEnv, signSessionToken, timingSafeEqual, verifySessionToken } from './session.mjs';

const ADMIN = 'a'.repeat(32);
const NONCE = 'A'.repeat(24);
const EXP = 4102444800; // 2100-01-01
const VECTOR_ADMIN = 'v1.4102444800.AAAAAAAAAAAAAAAAAAAAAAAA.RTY8v1ZRIjSHwEy-wdfVxWuBx8YWnZm0LUHw_dNwkU8';
const VECTOR_DEDICATED = 'v1.4102444800.AAAAAAAAAAAAAAAAAAAAAAAA.QPBAqzoV2MMafWhabFd-fD2qLD_IY5kE-HIh9VkU1cA';

test('derives the same key and token as the API (fixed vectors)', async () => {
  const key = await sessionKeyFromEnv({ ADMIN_SECRET: ADMIN });
  assert.equal(await signSessionToken(EXP, NONCE, key), VECTOR_ADMIN);
  const dedicated = await sessionKeyFromEnv({ ADMIN_SECRET: ADMIN, SESSION_SECRET: 's'.repeat(32) });
  assert.equal(await signSessionToken(EXP, NONCE, dedicated), VECTOR_DEDICATED);
});

test('accepts a valid, unexpired token', async () => {
  const key = await sessionKeyFromEnv({ ADMIN_SECRET: ADMIN });
  assert.deepEqual(await verifySessionToken(VECTOR_ADMIN, key, 1_800_000_000), { ok: true });
});

test('refuses a forged signature, a tampered expiry and a token signed with another key', async () => {
  const key = await sessionKeyFromEnv({ ADMIN_SECRET: ADMIN });
  const sig = VECTOR_ADMIN.split('.')[3];
  const flipped = VECTOR_ADMIN.slice(0, -1) + (VECTOR_ADMIN.endsWith('A') ? 'B' : 'A');
  assert.equal((await verifySessionToken(flipped, key, 1_800_000_000)).reason, 'bad-signature');
  const longer = `v1.4102444801.${NONCE}.${sig}`;
  assert.equal((await verifySessionToken(longer, key, 1_800_000_000)).reason, 'bad-signature');
  assert.equal((await verifySessionToken(VECTOR_DEDICATED, key, 1_800_000_000)).reason, 'bad-signature');
});

test('refuses an expired token even with a valid signature', async () => {
  const key = await sessionKeyFromEnv({ ADMIN_SECRET: ADMIN });
  const past = await signSessionToken(1_700_000_000, NONCE, key);
  assert.equal((await verifySessionToken(past, key, 1_800_000_000)).reason, 'expired');
  assert.equal((await verifySessionToken(VECTOR_ADMIN, key, EXP)).reason, 'expired'); // exp is exclusive
});

test('refuses malformed tokens and the old opaque format', async () => {
  const key = await sessionKeyFromEnv({ ADMIN_SECRET: ADMIN });
  for (const bad of ['', 'x', 'opaque-token-urlsafe-value-from-before', 'v2.4102444800.AAAAAAAAAAAAAAAAAAAAAAAA.sig',
    `v1.41024448OO.${NONCE}.sig`, `v1.4102444800.short.sig`, `v1.4102444800.${NONCE}`, 'v1.' + 'x'.repeat(300)]) {
    assert.equal((await verifySessionToken(bad, key, 1_800_000_000)).ok, false, bad);
  }
});

test('fails closed without a configured key', async () => {
  assert.equal(await sessionKeyFromEnv({}), null);
  assert.equal(await sessionKeyFromEnv({ ADMIN_SECRET: 'short' }), null);
  assert.equal(await sessionKeyFromEnv({ ADMIN_SECRET: ADMIN, SESSION_SECRET: 'short' }), null);
  assert.equal((await verifySessionToken(VECTOR_ADMIN, null, 1_800_000_000)).reason, 'no-key');
});

test('timingSafeEqual compares whole strings', () => {
  assert.equal(timingSafeEqual('abc', 'abc'), true);
  assert.equal(timingSafeEqual('abc', 'abd'), false);
  assert.equal(timingSafeEqual('abc', 'abcd'), false);
  assert.equal(timingSafeEqual('', 'a'), false);
});
