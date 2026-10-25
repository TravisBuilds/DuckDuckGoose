// Session token verification for the Next.js middleware (G-AUTH-1). Pure Web Crypto, so it runs in
// the Edge runtime and in Node (`node --test lib/`). Same format and key as api/main.py:
//   token = "v1.<expires_unix>.<nonce>.<base64url(HMAC-SHA256(key, 'v1.<expires_unix>.<nonce>'))>"
//   key   = SESSION_SECRET (>= 32 chars) if set, else HMAC-SHA256(ADMIN_SECRET, SESSION_KEY_CONTEXT)
// The API is still the authority (it also checks the DB row, so logout revokes); this check keeps
// forged / expired cookies away from every studio page.
const enc = new TextEncoder();
export const SESSION_KEY_CONTEXT = 'hfvg-studio-session-v1';
const NONCE_RE = /^[A-Za-z0-9_-]{16,64}$/;
const EXPIRY_RE = /^[0-9]{1,12}$/;

async function hmacSha256(keyBytes, message) {
  const key = await crypto.subtle.importKey('raw', keyBytes, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return new Uint8Array(await crypto.subtle.sign('HMAC', key, enc.encode(message)));
}

function base64url(bytes) {
  let s = '';
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** Constant-time string compare: every byte of the longer input is visited; lengths are folded in. */
export function timingSafeEqual(a, b) {
  const x = enc.encode(String(a));
  const y = enc.encode(String(b));
  let diff = x.length ^ y.length;
  const n = Math.max(x.length, y.length);
  for (let i = 0; i < n; i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}

/** Signing key from the server environment, or null when not configured (callers fail closed). */
export async function sessionKeyFromEnv(env) {
  const dedicated = env.SESSION_SECRET || '';
  if (dedicated) return dedicated.length >= 32 ? enc.encode(dedicated) : null;
  const admin = env.ADMIN_SECRET || env.STUDIO_ADMIN_SECRET || '';
  if (admin.length < 32) return null;
  return hmacSha256(enc.encode(admin), SESSION_KEY_CONTEXT);
}

export async function signSessionToken(expiresAt, nonce, key) {
  const payload = `v1.${expiresAt}.${nonce}`;
  return `${payload}.${base64url(await hmacSha256(key, payload))}`;
}

/**
 * Verify a session token: format, HMAC (constant-time compare) and expiry.
 * Returns { ok: true } or { ok: false, reason }.
 */
export async function verifySessionToken(token, key, nowSeconds = Date.now() / 1000) {
  if (!key) return { ok: false, reason: 'no-key' };
  if (typeof token !== 'string' || token.length === 0 || token.length > 256) return { ok: false, reason: 'malformed' };
  const parts = token.split('.');
  if (parts.length !== 4 || parts[0] !== 'v1' || !EXPIRY_RE.test(parts[1]) || !NONCE_RE.test(parts[2])) {
    return { ok: false, reason: 'malformed' };
  }
  const expected = await signSessionToken(parts[1], parts[2], key);
  if (!timingSafeEqual(expected, token)) return { ok: false, reason: 'bad-signature' };
  if (!(Number(parts[1]) > nowSeconds)) return { ok: false, reason: 'expired' };
  return { ok: true };
}
