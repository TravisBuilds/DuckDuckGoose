'use client';

import { useEffect, useId, useRef, type ReactNode } from 'react';
import { headline, promise, type MoneyProblem, type Price, type PriceRow } from '@/lib/checkout.mjs';
import { btnPrimary, btnSecondary, focusRing } from '@/lib/ui';

/**
 * Easy checkout (docs/EASY-CHECKOUT.md): the one price of a paid thing in big friendly type, one
 * button, an optional quiet "Not now", one calm line, and the details behind "See the breakdown".
 * It only shows a price the API gave: without one (price null) the button stays off, unless the action
 * spends almost nothing itself (needsPrice false: starting a video, whose story costs pennies and whose
 * pictures and clips are approved with its price at the shot plan). The caller sends that same price as
 * the approved total (lib/checkout.mjs approvedRequest), and the server refuses a higher one. A refused
 * spend shows in calm words with its one next action (moneyProblem) instead of the button and the
 * breakdown, so an old price can never be approved again or read as the current one. While the button
 * is off for want of a price that could not be read, `retry` adds a quiet "Try again". A card opened by
 * a tap (`reveal`) scrolls itself fully into view, above whatever is pinned over the bottom of a phone.
 */
export default function Checkout({
  testId, what, title, headlineTestId, price, action, onAction, busy = false, busyAction, off = null, whyTestId,
  disabled = false, describedBy, needsPrice = true, actionTestId, notNow, notNowTestId, retry, line, lineTestId, note,
  rows = [], children, problem = null, onProblem, reveal = false, className = '',
}: {
  testId: string;
  what: string;
  title?: string;
  headlineTestId?: string;
  price: Price | null;
  action: string;
  onAction: () => void;
  busy?: boolean;
  busyAction?: string;
  off?: string | null;
  whyTestId?: string;
  disabled?: boolean;
  describedBy?: string;
  needsPrice?: boolean;
  actionTestId?: string;
  notNow?: () => void;
  notNowTestId?: string;
  retry?: () => void;
  line?: string | null;
  lineTestId?: string;
  note?: ReactNode;
  rows?: PriceRow[];
  children?: ReactNode;
  problem?: Pick<MoneyProblem, 'text' | 'action'> | null;
  onProblem?: () => void;
  reveal?: boolean;
  className?: string;
}) {
  const whyId = useId();
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (reveal) box.current?.scrollIntoView({ block: 'nearest' });
  }, [reveal]);
  const calm = line === undefined ? promise(price) : line;
  const why = off ?? (price || !needsPrice ? null : 'Turns on once the price is on screen.');
  return (
    <div ref={box} className={`rounded-card border-2 border-sky bg-white p-4 sm:p-5 ${className}`} data-testid={testId}
      data-free={price ? String(price.free) : undefined}>
      <p className="font-display text-[22px] font-bold leading-tight tracking-[-0.01em] text-ink sm:text-[26px]"
        data-testid={headlineTestId ?? `${testId}-price`}>
        {problem ? what : title ?? headline(what, price)}
      </p>
      {note && <div className="mt-1.5 break-words text-[13.5px] text-ink-soft">{note}</div>}
      {problem ? (
        <div className="mt-3 rounded-inner bg-blue-soft p-3.5" role="alert" data-testid={`${testId}-problem`}>
          <p className="text-[14px] font-bold text-ink">{problem.text}</p>
          {onProblem && (
            <button type="button" className={`${btnPrimary} pf-btn-sm mt-2.5`} disabled={busy} onClick={onProblem}
              data-testid={`${testId}-problem-action`}>
              {problem.action}
            </button>
          )}
        </div>
      ) : (
        <>
          <div className="mt-3.5 flex flex-wrap items-center gap-2.5">
            <button type="button" className={btnPrimary} disabled={busy || disabled || Boolean(why)} onClick={onAction}
              aria-describedby={why ? whyId : describedBy} data-testid={actionTestId ?? `${testId}-action`}>
              {busy && busyAction ? busyAction : action}
            </button>
            {notNow && (
              <button type="button" className={`${btnSecondary} pf-btn-sm`} disabled={busy} onClick={notNow}
                data-testid={notNowTestId ?? `${testId}-not-now`}>
                Not now
              </button>
            )}
            {retry && why && (
              <button type="button" className={`${btnSecondary} pf-btn-sm`} disabled={busy} onClick={retry}
                data-testid={`${testId}-retry`}>
                Try again
              </button>
            )}
          </div>
          {why && <p id={whyId} className="pf-off-why" data-testid={whyTestId}>{why}</p>}
          {calm && <p className="mt-2.5 text-[13px] text-ink-soft" data-testid={lineTestId ?? `${testId}-line`}>{calm}</p>}
        </>
      )}
      {!problem && (rows.length > 0 || children) && (
        <details className="mt-2.5" data-testid={`${testId}-breakdown`}>
          <summary className={`w-fit cursor-pointer rounded text-[13px] font-extrabold text-blue-deep ${focusRing}`}>
            See the breakdown
          </summary>
          {rows.length > 0 && (
            <dl className="mt-2 space-y-1 text-[13px]">
              {rows.map((row) => (
                <div key={row.label} className="flex justify-between gap-3">
                  <dt>{row.label}</dt>
                  <dd className="text-right font-extrabold">{row.value}</dd>
                </div>
              ))}
            </dl>
          )}
          {children && <div className="mt-2 space-y-1 break-words text-[12.5px] text-ink-soft">{children}</div>}
        </details>
      )}
    </div>
  );
}
