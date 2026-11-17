import type { ReactNode } from 'react';
import Mascot, { type MascotKind } from './Mascot';

/** A dashed "nothing here yet" card with a mascot, one sentence and one way forward. */
export default function EmptyState({ mascot = 'duckling', title, children, action, testId, className = '' }: {
  mascot?: MascotKind; title: ReactNode; children?: ReactNode; action?: ReactNode; testId?: string; className?: string;
}) {
  return (
    <div data-testid={testId}
      className={`grid place-items-center rounded-card border-[3px] border-dashed border-[#D9D1BF] px-6 py-8 text-center ${className}`}>
      <div className="flex max-w-md flex-col items-center">
        <Mascot kind={mascot} className="h-24 w-24" />
        <h3 className="mt-2 text-[19px]">{title}</h3>
        {children && <div className="mt-1 text-[13px] text-ink-soft">{children}</div>}
        {action && <div className="mt-4">{action}</div>}
      </div>
    </div>
  );
}
