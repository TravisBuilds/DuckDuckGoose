import type { ReactNode } from 'react';

export interface GateItem {
  key: string;
  label: ReactNode;
  done: boolean;
  detail?: ReactNode;
}

/** What an approval or start waits on: a filled lime circle or an open orange-red ring per line, with the words for both. */
export default function GateChecklist({ items, testId, className = '' }: { items: GateItem[]; testId?: string; className?: string }) {
  return (
    <ul className={`space-y-2 ${className}`} data-testid={testId}>
      {items.map((item) => (
        <li key={item.key} className="flex items-start gap-2.5 text-[13.5px]" data-testid={testId ? `${testId}-${item.key}` : undefined}
          data-done={item.done ? 'true' : 'false'}>
          {item.done ? (
            <span aria-hidden="true" className="mt-px size-5 flex-none rounded-full border-[2.5px] border-green-deep bg-green" />
          ) : (
            <span aria-hidden="true" className="mt-px size-5 flex-none rounded-full border-[2.5px] border-coral bg-white" />
          )}
          <span className="min-w-0">
            <span className="sr-only">{item.done ? 'Done: ' : 'Not yet: '}</span>
            <span className="font-bold">{item.label}</span>
            {item.detail && <span className="block text-[12.5px] text-ink-soft">{item.detail}</span>}
          </span>
        </li>
      ))}
    </ul>
  );
}
