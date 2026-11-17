import Icon, { type IconName } from './Icon';

const TONES = {
  blue: 'from-blue-soft to-aqua text-blue-ink',
  lime: 'from-green-soft to-green text-ink',
  sun: 'from-mustard-soft to-mustard text-ink',
} as const;

/** A line icon in a soft gradient circle: where the old illustrations stood (empty states, card headers, dead ends). Decorative. */
export default function IconCircle({ icon, size = 56, tone = 'blue', className = '' }: {
  icon: IconName; size?: number; tone?: keyof typeof TONES; className?: string;
}) {
  return (
    <span aria-hidden="true" style={{ width: size, height: size }}
      className={`grid flex-none place-items-center rounded-full bg-gradient-to-br shadow-card ${TONES[tone]} ${className}`}>
      <Icon name={icon} size={Math.round(size * 0.42)} />
    </span>
  );
}
