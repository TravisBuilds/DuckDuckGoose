import type { HTMLAttributes, ReactNode } from 'react';

export type StickyColour = 'yellow' | 'pink' | 'blue' | 'green';

const TILTS = [-1.5, 1, -0.5, 1.5, -1, 0.5];

/** The sticky-note colour for each kind of reference note. */
export const TOPIC_STICKY: Record<string, StickyColour> = {
  premise: 'yellow',
  characters: 'pink',
  places: 'blue',
  tone: 'green',
};

/** A paper sticky note (references, the mood board's notes): a little tilt, a curled corner, a bold title. */
export default function StickyNote({ colour = 'yellow', title, index = 0, className = '', children, ...rest }:
  Omit<HTMLAttributes<HTMLDivElement>, 'title'> & { colour?: StickyColour; title?: ReactNode; index?: number }) {
  return (
    <div className={`pf-sticky pf-sticky-${colour} ${className}`} style={{ transform: `rotate(${TILTS[index % TILTS.length]}deg)` }}
      {...rest}>
      {title && <b>{title}</b>}
      {children}
    </div>
  );
}
