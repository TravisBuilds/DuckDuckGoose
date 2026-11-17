import type { CastHue } from '@/lib/castColour.mjs';
import { CAST_COLOURS } from '@/lib/castColour.mjs';
import { CAST_GRADIENTS } from '@/lib/palette.mjs';

/** Up to two initials from a name or email ("Mira Vale" → "MV", "wren@x.io" → "W"); "?" when there is nothing. */
export function initialsOf(name: string | null | undefined): string {
  const words = String(name ?? '').split('@')[0].split(/[\s._-]+/).filter((word) => /\p{L}|\p{N}/u.test(word));
  const letters = words.slice(0, 2).map((word) => Array.from(word.replace(/^[^\p{L}\p{N}]+/u, ''))[0] ?? '');
  return letters.join('').toUpperCase() || '?';
}

/** A steady gradient for someone without a cast colour (a member, a Universe): the same name always gets the same one. */
export function hueForName(name: string | null | undefined): CastHue {
  let hash = 0;
  for (const ch of String(name ?? '')) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return CAST_COLOURS[hash % CAST_COLOURS.length];
}

/**
 * Someone (or something) as a circle: their initials in ink on a soft gradient of their cast colour.
 * Decorative: the name always sits next to it in words.
 */
export default function Avatar({ name, hue, size = 40, className = '', testId }: {
  name: string | null | undefined;
  hue?: CastHue | 'ink';
  size?: number;
  className?: string;
  testId?: string;
}) {
  const [from, to] = CAST_GRADIENTS[hue ?? hueForName(name)];
  return (
    <span aria-hidden="true" data-testid={testId} className={`pf-avatar ${className}`}
      style={{ width: size, height: size, fontSize: Math.max(11, Math.round(size * 0.36)), background: `linear-gradient(145deg, ${from}, ${to})` }}>
      {initialsOf(name)}
    </span>
  );
}
