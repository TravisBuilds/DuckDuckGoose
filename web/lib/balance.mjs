/**
 * Higgsfield balance line for the credits panel (B8 / GC.01).
 *
 * The backend reports the manually recorded balance (`higgsfield_balance` in
 * GET /api/episodes/{id}/credits): integer usd_micros, when it was recorded and whether it is
 * recent enough for a live run (HFVG_BALANCE_MAX_AGE_HOURS). Integer arithmetic only.
 */

/** "$12.34" from integer usd_micros (half-up to cents). */
export function formatUsdMicros(micros) {
  const negative = micros < 0;
  const abs = Math.abs(Math.round(micros));
  const cents = Math.floor((abs + 5_000) / 10_000);
  const whole = Math.floor(cents / 100);
  const frac = String(cents % 100).padStart(2, '0');
  return `${negative ? '-' : ''}$${whole.toLocaleString('en-US')}.${frac}`;
}

/**
 * Display model for the balance line.
 * @returns {{ text: string, tone: 'ok' | 'warn' | 'missing', liveBlocked: boolean }}
 */
export function balanceDisplay(balance) {
  if (!balance || balance.balance_usd_micros === null || balance.balance_usd_micros === undefined) {
    return { text: 'Higgsfield balance: not recorded (live runs refused)', tone: 'missing', liveBlocked: true };
  }
  const amount = formatUsdMicros(balance.balance_usd_micros);
  const age = typeof balance.age_hours === 'number' ? `${balance.age_hours.toFixed(1)} h ago` : 'unknown age';
  if (!balance.fresh) {
    const max = typeof balance.max_age_hours === 'number' ? ` (max ${balance.max_age_hours} h)` : '';
    return { text: `Higgsfield balance: ${amount}, recorded ${age}${max}: too old, live runs refused`, tone: 'warn', liveBlocked: true };
  }
  return { text: `Higgsfield balance: ${amount}, recorded ${age}`, tone: 'ok', liveBlocked: false };
}

/** Validate a typed USD amount ("12.50") for POST /api/budget/balance; returns the string or null. */
export function parseBalanceInput(raw) {
  const v = String(raw ?? '').trim();
  return /^\d{1,7}(\.\d{1,2})?$/.test(v) ? v : null;
}
