'use client';

import Link from 'next/link';
import { useEffect, useRef } from 'react';
import Icon from './Icon';

export type StopState = 'done' | 'now' | 'todo' | 'blocked';

export interface JourneyStop {
  key: string;
  name: string;
  note?: string | null;
  state: StopState;
  /** A step that spends money once approved: it wears a "$" coin. */
  paid?: boolean;
  href?: string | null;
}

const STATE_WORDS: Record<StopState, string> = {
  done: 'done',
  now: 'you are here',
  todo: 'not started',
  blocked: 'blocked',
};

/**
 * The board-game path of numbered stops (DESIGN-TARGETS-v2 §3): green check bubbles for done
 * stops, a pulsing mustard bubble for the current one, dashed stops ahead and a coral one when
 * blocked; dashed links fill green behind you. Used for the five Universe steps and the episode
 * trail (with coins on paid steps). Scrolls sideways inside itself on narrow screens, with the
 * current stop centred.
 */
export default function JourneyPath({ stops, current, label, compact = false, testId, className = '' }: {
  stops: JourneyStop[];
  current?: string | null;
  label: string;
  compact?: boolean;
  testId?: string;
  className?: string;
}) {
  const strip = useRef<HTMLElement>(null);
  const here = current ?? stops.find((stop) => stop.state === 'now')?.key ?? null;
  useEffect(() => {
    const nav = strip.current;
    const stop = nav?.querySelector<HTMLElement>('[aria-current="step"]');
    if (!nav || !stop || nav.scrollWidth <= nav.clientWidth) return;
    nav.scrollLeft = stop.offsetLeft - (nav.clientWidth - stop.offsetWidth) / 2;
  }, [here]);
  return (
    <nav ref={strip} aria-label={label} data-testid={testId} className={`relative overflow-x-auto pb-1 ${className}`}>
      <ol className={`pf-path min-w-max ${compact ? 'is-compact' : ''}`}>
        {stops.map((stop, i) => {
          const isCurrent = current ? stop.key === current : stop.state === 'now';
          const body = (
            <>
              <span className="pf-bub" aria-hidden="true">
                {stop.state === 'done' ? <Icon name="check" size={18} />
                  : stop.state === 'blocked' ? <Icon name="lock" size={17} /> : i + 1}
                {stop.paid && <span className="pf-coin">$</span>}
              </span>
              <span className="pf-stop-name">{stop.name}</span>
              {stop.note && <span className="pf-stop-note">{stop.note}</span>}
              <span className="sr-only">
                {`, step ${i + 1} of ${stops.length}, ${STATE_WORDS[stop.state]}${stop.paid ? ', costs money' : ''}`}
              </span>
            </>
          );
          const cls = `pf-stop is-${stop.state}`;
          return (
            <li key={stop.key} data-testid={testId ? `${testId}-${stop.key}` : undefined} data-state={stop.state}
              className={`flex items-start ${i === 0 ? 'flex-none' : 'flex-1'}`}>
              {i > 0 && <span aria-hidden="true" className={`pf-link ${stops[i - 1].state === 'done' ? 'is-done' : ''}`} />}
              {stop.href ? (
                <Link href={stop.href} className={cls} aria-current={isCurrent ? 'step' : undefined}>{body}</Link>
              ) : (
                <span className={cls} aria-current={isCurrent ? 'step' : undefined}>{body}</span>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
