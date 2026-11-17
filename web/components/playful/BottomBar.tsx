import Link from 'next/link';
import type { ReactNode } from 'react';

export interface BottomItem {
  key: string;
  label: string;
  href?: string;
  icon: ReactNode;
  active?: boolean;
  onClick?: () => void;
  expanded?: boolean;
}

/**
 * The phone navigation (DESIGN-TARGETS-v2 §4): a glass pill fixed to the bottom with big targets
 * and the coral "make something" button raised in the middle. Hidden from 640 px up.
 */
export default function BottomBar({ items, fab, label = 'Studio', testId }: {
  items: BottomItem[]; fab?: ReactNode; label?: string; testId?: string;
}) {
  const half = Math.ceil(items.length / 2);
  const render = (item: BottomItem) => {
    const cls = `flex min-h-11 min-w-14 flex-col items-center justify-center gap-0.5 rounded-2xl px-2 text-[11px] font-extrabold ${item.active ? 'text-blue-deep' : 'text-ink-soft'}`;
    return item.href ? (
      <Link key={item.key} href={item.href} className={cls} aria-current={item.active ? 'page' : undefined}
        data-testid={testId ? `${testId}-${item.key}` : undefined}>
        {item.icon}{item.label}
      </Link>
    ) : (
      <button key={item.key} type="button" onClick={item.onClick} className={cls} aria-expanded={item.expanded}
        data-testid={testId ? `${testId}-${item.key}` : undefined}>
        {item.icon}{item.label}
      </button>
    );
  };
  return (
    <nav aria-label={label} data-testid={testId}
      className="pf-glass fixed inset-x-3 bottom-3 z-40 flex items-center justify-around !rounded-full px-2 py-2 sm:hidden">
      {items.slice(0, half).map(render)}
      {fab}
      {items.slice(half).map(render)}
    </nav>
  );
}
