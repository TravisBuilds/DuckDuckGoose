import type { HTMLAttributes, ReactNode } from 'react';

export type StickyColour = 'yellow' | 'peach' | 'blue' | 'green';

/** The note colour for each kind of reference note. */
export const TOPIC_STICKY: Record<string, StickyColour> = {
  premise: 'yellow',
  characters: 'peach',
  places: 'blue',
  tone: 'green',
};

/** A note card (references, the mood board's notes): a soft accent tint, rounded corners, a bold title. */
export default function StickyNote({ colour = 'yellow', title, className = '', children, ...rest }:
  Omit<HTMLAttributes<HTMLDivElement>, 'title'> & { colour?: StickyColour; title?: ReactNode }) {
  return (
    <div className={`pf-sticky pf-sticky-${colour} ${className}`} {...rest}>
      {title && <b>{title}</b>}
      {children}
    </div>
  );
}
