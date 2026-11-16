/**
 * Dates from the API. The studio database stores UTC (SQLite CURRENT_TIMESTAMP, "2026-11-15 07:56:56")
 * without a zone, which browsers read as local time, so a video made late in the evening showed the
 * wrong day. These helpers read zone-less timestamps as UTC and show them in the viewer's zone.
 * No imports: lib/time.test.mjs loads this file on its own.
 */

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;
const HAS_ZONE = /(Z|[+-]\d{2}:?\d{2})$/i;

/** The instant an API timestamp names; null for a missing or unreadable value. */
export function parseApiDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const text = value.trim();
  if (DATE_ONLY.test(text)) {
    // a calendar day, not an instant: keep the same day in every zone
    const [y, m, d] = text.split('-').map(Number);
    return new Date(y, m - 1, d);
  }
  let iso = text.replace(' ', 'T');
  if (!HAS_ZONE.test(iso)) iso += 'Z';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** "Nov 15, 2026" in the viewer's zone (or the given one); '' when the value cannot be read. */
export function formatLocalDate(value: string | null | undefined, locale?: string, timeZone?: string): string {
  const date = parseApiDate(value);
  if (!date) return '';
  return date.toLocaleDateString(locale, { year: 'numeric', month: 'short', day: 'numeric', timeZone });
}

/** "Nov 15, 2026, 9:56 AM" in the viewer's zone (or the given one). */
export function formatLocalDateTime(value: string | null | undefined, locale?: string, timeZone?: string): string {
  const date = parseApiDate(value);
  if (!date) return '';
  return date.toLocaleString(locale, {
    year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone,
  });
}
