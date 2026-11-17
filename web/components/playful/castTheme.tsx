import type { CastColour, CastHue } from '@/lib/castColour.mjs';

/**
 * Class names for each cast colour (DESIGN-TARGETS-v2 §1.4): the soft fill, the readable text
 * shade on it, the bright dot, the figure tint and the card band. Spelled out in full so Tailwind
 * sees every class.
 */
export const HUES: Record<CastHue | 'ink', {
  name: string; soft: string; ink: string; dot: string; fig: string; band: [string, string];
}> = {
  blue: { name: 'Blue', soft: 'bg-blue-soft', ink: 'text-blue-ink', dot: 'bg-blue', fig: 'text-blue', band: ['#DCE8F7', '#A9C6EA'] },
  mustard: { name: 'Mustard', soft: 'bg-mustard-soft', ink: 'text-mustard-ink', dot: 'bg-mustard', fig: 'text-mustard', band: ['#FBF1D6', '#EBD49A'] },
  aqua: { name: 'Aqua', soft: 'bg-aqua-soft', ink: 'text-aqua-ink', dot: 'bg-aqua', fig: 'text-aqua', band: ['#E2F4F7', '#A6DCE5'] },
  coral: { name: 'Coral', soft: 'bg-coral-soft', ink: 'text-coral-ink', dot: 'bg-coral', fig: 'text-coral', band: ['#FDE3E0', '#F4B3AB'] },
  green: { name: 'Green', soft: 'bg-green-soft', ink: 'text-green-ink', dot: 'bg-green', fig: 'text-green', band: ['#E3F3E4', '#A9D9AB'] },
  lilac: { name: 'Lilac', soft: 'bg-lilac-soft', ink: 'text-lilac-ink', dot: 'bg-lilac', fig: 'text-lilac', band: ['#EEE8F9', '#C9B8EA'] },
  ink: { name: 'Narrator', soft: 'bg-[#EDEFF4]', ink: 'text-ink', dot: 'bg-ink-soft', fig: 'text-[#9AA6B8]', band: ['#EDEFF4', '#C9CED9'] },
};

export function hueOf(colour: CastColour | null | undefined) {
  return HUES[colour?.colour ?? 'ink'];
}

export function bandStyle(colour: CastColour | null | undefined) {
  const [from, to] = hueOf(colour).band;
  return { background: `linear-gradient(160deg, ${from}, ${to})` };
}

/** The colour dot that always sits next to a name (never colour alone); striped from the seventh character. */
export function CastDot({ colour, className = '' }: { colour: CastColour | null | undefined; className?: string }) {
  const hue = hueOf(colour);
  return (
    <span aria-hidden="true"
      className={`pf-dot ${hue.dot} ${colour?.patterned ? `pf-dot-striped ${hue.ink}` : ''} ${className}`} />
  );
}
