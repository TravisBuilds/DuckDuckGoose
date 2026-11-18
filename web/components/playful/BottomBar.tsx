import Link from 'next/link';
import type { ReactNode } from 'react';

export interface BottomItem {
  key: string;
  label: string;
  href?: string;
  active?: boolean;
  onClick?: () => void;
  expanded?: boolean;
  /** The menu this button toggles (it is not a click outside that menu). */
  menu?: string;
}

/**
 * The phone navigation: a white rounded tab bar fixed to the bottom, each tab its name in words with
 * an azure bar under the one you are on, big targets, and the azure "make something" button raised in
 * the middle. Hidden from 640 px up.
 */
export default function BottomBar({ items, fab, label = 'Studio', testId }: {
  items: BottomItem[]; fab?: ReactNode; label?: string; testId?: string;
}) {
  const half = Math.ceil(items.length / 2);
  const render = (item: BottomItem) => {
    const on = Boolean(item.active || item.expanded);
    // each tab is as wide as its word plus an even share of the room left, so "Universes" fits beside
    // "Episodes" on a 320 px phone
    const cls = `flex min-h-12 min-w-0 flex-auto flex-col items-center justify-center gap-1.5 rounded-2xl px-0.5 pt-1 font-display text-[clamp(10.5px,3.1vw,13px)] font-bold leading-none tracking-[-0.01em] ${on ? 'text-blue-deep' : 'text-ink-soft'}`;
    const body = (
      <>
        <span className="whitespace-nowrap">{item.label}</span>
        <span aria-hidden="true" className={`h-1 w-6 rounded-full ${on ? 'bg-blue-deep' : 'bg-transparent'}`} />
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
      className="fixed inset-x-3 bottom-3 z-40 flex items-center gap-1 rounded-[28px] border border-line/70 bg-white px-2 py-1.5 shadow-float sm:hidden">
      {items.slice(0, half).map(render)}
      {fab}
      {items.slice(half).map(render)}
    </nav>
  );
}
