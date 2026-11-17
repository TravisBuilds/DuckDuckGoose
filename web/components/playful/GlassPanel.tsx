import type { HTMLAttributes } from 'react';

/**
 * The one primary glass panel on a screen (the start panel, the consistency panel, the price or
 * approval panel). Without backdrop-filter it falls back to a near-opaque white (globals.css).
 */
export default function GlassPanel({ as: Tag = 'div', className = '', ...rest }: HTMLAttributes<HTMLElement> & {
  as?: 'div' | 'section' | 'aside';
}) {
  return <Tag className={`pf-glass ${className}`} {...rest} />;
}
