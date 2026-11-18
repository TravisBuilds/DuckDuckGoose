import { ACCENTS, ICONS } from '@/lib/icons.mjs';
import Icon, { type IconName } from './Icon';

const TONES = { blue: ACCENTS.sky.fill, lime: ACCENTS.lime.fill, sun: ACCENTS.yellow.fill } as const;

/**
 * A duotone icon on a white sticker disc with a crescent of colour set off behind it (the icon's own
 * accent, or `tone` when the colour says something: yellow for "needs you"). Stands where the old
 * illustrations stood: empty states, card and section headers, dead ends. Decorative.
 */
export default function IconCircle({ icon, size = 56, tone, className = '' }: {
  icon: IconName; size?: number; tone?: keyof typeof TONES; className?: string;
}) {
  const tint = tone ? TONES[tone] : ACCENTS[ICONS[icon].accent].fill;
  const offset = Math.max(3, Math.round(size / 14));
  return (
    <span aria-hidden="true"
      style={{ width: size, height: size, boxShadow: `inset 0 0 0 1.5px var(--line), ${offset}px ${offset}px 0 0 ${tint}, var(--shadow-1)` }}
      className={`grid flex-none place-items-center rounded-full bg-white text-ink ${className}`}>
      <Icon name={icon} size={Math.round(size * 0.5)} />
    </span>
  );
}
