/**
 * Shared studio class sets, in the "Playful Universe" look (app/globals.css). Every control keeps the
 * same visible keyboard focus ring (3px blue-deep outline, 2px offset), so tabbing through a page
 * always shows where you are.
 */
export const focusRing = 'focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-blue-deep';

const button = `pf-btn ${focusRing}`;

/** Go somewhere / do a non-spending thing (blue). */
export const btnPrimary = `${button} pf-btn-primary`;
export const btnSecondary = `${button} pf-btn-quiet`;
/** Destructive, always behind a confirm (coral). */
export const btnDanger = `${button} pf-btn-danger`;
/** Approve or spend (green). */
export const btnGo = `${button} pf-btn-go`;
export const btnGhost = `${button} pf-btn-ghost`;
export const btnSoft = `${button} pf-btn-soft`;
export const btnSm = 'pf-btn-sm';

/** A card without padding, for cards with their own header and body sections. */
export const panel = 'rounded-card border border-[#F0EADF] bg-paper shadow-card';
export const card = `${panel} p-5 sm:p-6`;
/** The card that needs attention first (the next step, the finished video). */
export const highlight = 'rounded-card border-2 border-mustard bg-paper p-5 shadow-card sm:p-6';
/** The one primary glass panel on a screen (start panel, consistency panel, price/gate panel). */
export const glass = 'pf-glass';
/** An inner block inside a card. */
export const inner = 'rounded-inner bg-white';

export const input = `pf-input ${focusRing}`;
export const label = 'pf-label';
export const eyebrow = 'pf-eyebrow';

export const link = `pf-link-text ${focusRing}`;

/** Small status pill; pair with one of the tones below. */
export const chip = 'pf-chip';

/**
 * One meaning per colour (DESIGN-TARGETS-v2 §1.3): green done/locked, mustard a decision waiting
 * for you, aqua the studio working, coral stopped, blue plain information.
 */
export const tones = {
  ok: 'bg-green-soft text-green-ink',
  todo: 'bg-mustard-soft text-mustard-ink',
  run: 'bg-aqua-soft text-aqua-ink',
  wait: 'bg-[#EFEDE7] text-ink-soft',
  warn: 'bg-mustard-soft text-mustard-ink',
  bad: 'bg-coral-soft text-coral-ink',
  info: 'bg-blue-soft text-blue-ink',
} as const;

export type Tone = keyof typeof tones;
