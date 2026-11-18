import type { ReactNode } from 'react';
import Dots from './Dots';

/** A dashed "nothing here yet" card: three brand dots, one sentence and one way forward. */
export default function EmptyState({ title, children, action, testId, className = '' }: {
  title: ReactNode; children?: ReactNode; action?: ReactNode; testId?: string; className?: string;
}) {
  return (
    <div data-testid={testId}
      className={`grid place-items-center rounded-card border-2 border-dashed border-line-strong bg-white/60 px-6 py-8 text-center ${className}`}>
      <div className="flex max-w-md flex-col items-center">
        <Dots />
        <h3 className="mt-3 text-[18px]">{title}</h3>
        {children && <div className="mt-1 text-[13px] text-ink-soft">{children}</div>}
        {action && <div className="mt-4">{action}</div>}
      </div>
    </div>
  );
}
