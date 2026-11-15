/**
 * Shared studio class sets. Every control gets the same visible keyboard focus ring
 * (focus-visible:ring-2), so tabbing through a page always shows where you are.
 */
export const focusRing =
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-600 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-gray-900';

const button = `inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`;

export const btnPrimary = `${button} bg-blue-600 text-white hover:bg-blue-700`;
export const btnSecondary = `${button} border border-gray-300 bg-white text-gray-900 hover:bg-gray-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100 dark:hover:bg-gray-700`;
export const btnDanger = `${button} bg-red-600 text-white hover:bg-red-700`;

export const card = 'rounded-lg border border-gray-200 bg-white p-4 shadow-sm sm:p-6 dark:border-gray-700 dark:bg-gray-800';

export const input = `w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100 ${focusRing}`;

export const link = `rounded text-blue-700 underline-offset-2 hover:underline dark:text-blue-300 ${focusRing}`;

/** Small status pill; pair with one of the tones below. */
export const chip = 'inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold';

export const tones = {
  ok: 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-200',
  todo: 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-200',
  wait: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-200',
  warn: 'bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200',
  bad: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-200',
} as const;

export type Tone = keyof typeof tones;
