import type { ReactNode } from 'react';
import type { IconName } from './Icon';
import IconCircle from './IconCircle';

/** A dashed "nothing here yet" card with a duotone icon on a sticker disc, one sentence and one way forward. */
export default function EmptyState({ icon = 'spark', title, children, action, testId, className = '' }: {
  icon?: IconName; title: ReactNode; children?: ReactNode; action?: ReactNode; testId?: string; className?: string;
}) {
  return (
    <div data-testid={testId}
      className={`grid place-items-center rounded-card border-2 border-dashed border-line-strong bg-white/60 px-6 py-8 text-center ${className}`}>
      <div className="flex max-w-md flex-col items-center">
        <IconCircle icon={icon} size={64} />
        <h3 className="mt-3 text-[18px]">{title}</h3>
        {children && <div className="mt-1 text-[13px] text-ink-soft">{children}</div>}
        {action && <div className="mt-4">{action}</div>}
      </div>
    </div>
  );
}
