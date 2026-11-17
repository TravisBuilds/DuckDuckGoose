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
  /** The menu this button toggles (it is not a click outside that menu). */
  menu?: string;
}

/**
 * The phone navigation: a white rounded tab bar fixed to the bottom with line icons, big targets and
 * the azure "make something" button raised in the middle. Hidden from 640 px up.
 */
export default function BottomBar({ items, fab, label = 'Studio', testId }: {
  items: BottomItem[]; fab?: ReactNode; label?: string; testId?: string;
}) {
  const half = Math.ceil(items.length / 2);
  const render = (item: BottomItem) => {
    const cls = `group flex min-h-11 min-w-14 flex-col items-center justify-center gap-0.5 rounded-2xl px-2 font-display text-[10.5px] font-semibold ${item.active ? 'text-blue-deep' : 'text-ink-soft'}`;
    const body = (
      <>
        <span className={`grid h-7 w-11 place-items-center rounded-full ${item.active ? 'bg-blue-soft' : ''}`}>{item.icon}</span>
        {item.label}
      </>
    );
    return item.href ? (
      <Link key={item.key} href={item.href} className={cls} aria-current={item.active ? 'page' : undefined}
        data-testid={testId ? `${testId}-${item.key}` : undefined}>
        {body}
      </Link>
    ) : (
      <button key={item.key} type="button" onClick={item.onClick} className={cls} aria-expanded={item.expanded}
        data-menu={item.menu} data-testid={testId ? `${testId}-${item.key}` : undefined}>
        {body}
      </button>
    );
  };
  return (
    <nav aria-label={label} data-testid={testId}
      className="fixed inset-x-3 bottom-3 z-40 flex items-center justify-around rounded-[28px] border border-line/70 bg-white px-2 py-1.5 shadow-float sm:hidden">
      {items.slice(0, half).map(render)}
      {fab}
      {items.slice(half).map(render)}
    </nav>
  );
}
