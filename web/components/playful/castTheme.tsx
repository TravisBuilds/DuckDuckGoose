import type { CastColour, CastHue } from '@/lib/castColour.mjs';
import { CAST_GRADIENTS } from '@/lib/palette.mjs';

/**
 * Class names for each cast colour: the soft fill, the readable text shade on it and the bright dot,
 * with the colour's name in words and its soft gradient (lib/palette.mjs CAST_GRADIENTS) for the
 * initials circle and the card band. Spelled out in full so Tailwind sees every class.
 */
export const HUES: Record<CastHue | 'ink', {
  name: string; soft: string; ink: string; dot: string; band: [string, string];
}> = {
  blue: { name: 'Blue', soft: 'bg-blue-soft', ink: 'text-blue-ink', dot: 'bg-blue', band: CAST_GRADIENTS.blue },
  mustard: { name: 'Yellow', soft: 'bg-mustard-soft', ink: 'text-mustard-ink', dot: 'bg-mustard', band: CAST_GRADIENTS.mustard },
  aqua: { name: 'Cyan', soft: 'bg-aqua-soft', ink: 'text-aqua-ink', dot: 'bg-aqua', band: CAST_GRADIENTS.aqua },
  coral: { name: 'Orange', soft: 'bg-coral-soft', ink: 'text-coral-ink', dot: 'bg-coral', band: CAST_GRADIENTS.coral },
  green: { name: 'Lime', soft: 'bg-green-soft', ink: 'text-green-ink', dot: 'bg-green', band: CAST_GRADIENTS.green },
  lilac: { name: 'Lilac', soft: 'bg-lilac-soft', ink: 'text-lilac-ink', dot: 'bg-lilac', band: CAST_GRADIENTS.lilac },
  ink: { name: 'Narrator', soft: 'bg-mist', ink: 'text-ink', dot: 'bg-ink-soft', band: CAST_GRADIENTS.ink },
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
