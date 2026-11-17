'use client';

import { useEffect, useRef, useState } from 'react';

export type SealState = 'locked' | 'open' | 'drift';

const CONFETTI = ['var(--coral)', 'var(--mustard)', 'var(--blue)', 'var(--aqua)', 'var(--green)'];
const BURST = Array.from({ length: 10 }, (_, i) => {
  const angle = (i / 10) * Math.PI * 2;
  return { dx: Math.round(Math.cos(angle) * 54), dy: Math.round(Math.sin(angle) * 54), colour: CONFETTI[i % 5] };
});

/**
 * The round badge for locks: lime when locked, a dashed open ring before, orange-red when a locked
 * look changed. When it turns locked while on screen it plays the seal moment once (a stamp and a
 * small burst of the five swatches);
 * with reduced motion that is a short fade and no burst.
 */
export default function Seal({ state, top, bottom, size = 64, className = '', testId, title }: {
  state: SealState;
  top?: string;
  bottom?: string;
  size?: number;
  className?: string;
  testId?: string;
  title?: string;
}) {
  const previous = useRef(state);
  const [stamping, setStamping] = useState(false);
  useEffect(() => {
    const was = previous.current;
    previous.current = state;
    if (was === state || state !== 'locked') return undefined;
    setStamping(true);
    const timer = setTimeout(() => setStamping(false), 700);
    return () => clearTimeout(timer);
  }, [state]);

  const words = top !== undefined || bottom !== undefined
    ? [top, bottom]
    : state === 'locked' ? ['locked', ''] : state === 'drift' ? ['look', 'changed'] : ['not', 'locked'];
  const sized = size === 64 ? undefined : { width: size, height: size, fontSize: size < 60 ? 10 : 11 };
  return (
    <span data-testid={testId} data-state={state} title={title} style={sized}
      className={`pf-seal relative ${state === 'open' ? 'is-open' : state === 'drift' ? 'is-drift' : ''} ${stamping ? 'is-stamping' : ''} ${className}`}>
      <span>
        {words[0]}
        {words[1] ? <><br />{words[1]}</> : null}
      </span>
      {stamping && (
        <span className="pf-confetti" aria-hidden="true">
          {BURST.map((piece, i) => (
            <i key={i} style={{ background: piece.colour, ['--dx' as string]: `${piece.dx}px`, ['--dy' as string]: `${piece.dy}px` }} />
          ))}
        </span>
      )}
    </span>
  );
}
