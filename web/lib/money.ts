/**
 * Dollar formatting for the budget ledger.
 *
 * The backend reports every amount as an integer number of micro-dollars (usd_micros,
 * 1 USD = 1_000_000). Format with integer arithmetic only (no float division) so the UI never
 * shows drift such as 47.99999.
 */
export const MICROS_PER_USD = 1_000_000;

/** Backend default episode cap (hfvg/budget.py EPISODE_CAP_USD_MICROS): 60.00 USD. */
export const DEFAULT_EPISODE_CAP_USD_MICROS = 60_000_000;

/** Format integer usd_micros as "$12.34" (rounded half-up to cents; "-$x" for negatives). */
export function formatUsd(micros: number): string {
  const negative = micros < 0;
  const abs = Math.abs(Math.round(micros));
  const cents = Math.floor((abs + 5_000) / 10_000);
  const whole = Math.floor(cents / 100);
  const frac = String(cents % 100).padStart(2, '0');
  return `${negative ? '-' : ''}$${whole.toLocaleString('en-US')}.${frac}`;
}
