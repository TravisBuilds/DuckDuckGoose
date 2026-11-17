import type { CSSProperties, ReactNode } from 'react';

const PINS = ['var(--blue)', 'var(--green)', 'var(--coral)', 'var(--mustard)', 'var(--aqua)', 'var(--lilac)'];

export interface PinnedPicture {
  key: string;
  src: string;
  alt: string;
  href?: string;
  caption?: string | null;
  note?: string | null;
}

/** One pinned picture: a white rounded card with a coloured pin; drops in (staggered) when `dropping`. `children` sit under it. */
export function Polaroid({ picture, index = 0, dropping = false, children }: {
  picture: PinnedPicture; index?: number; dropping?: boolean; children?: ReactNode;
}) {
  const style = { animationDelay: dropping ? `${index * 80}ms` : undefined } as CSSProperties;
  const img = <img src={picture.src} alt={picture.alt} loading="lazy" className="aspect-[4/3] w-full rounded-[10px] bg-mist object-cover" />;
  return (
    <li className="min-w-0">
      <div className={`pf-polaroid ${dropping ? 'is-dropping' : ''}`} style={style}>
        <i className="pf-pin" style={{ background: PINS[index % PINS.length] }} aria-hidden="true" />
        {picture.href ? <a href={picture.href} target="_blank" rel="noreferrer" className="block" title="Open the full picture">{img}</a> : img}
        {picture.caption && <span className="pf-polaroid-cap">{picture.caption}</span>}
      </div>
      {picture.note && <p className="mt-1.5 text-[11px] font-extrabold text-ink-soft">{picture.note}</p>}
      {children}
    </li>
  );
}

/** The board: pictures pinned in a grid on a light-blue dotted panel, with room for notes after them. */
export default function PinBoard({ pictures, dropping = false, columns = 'grid-cols-2 sm:grid-cols-4', testId, children, className = '' }: {
  pictures: PinnedPicture[]; dropping?: boolean; columns?: string; testId?: string; children?: ReactNode; className?: string;
}) {
  return (
    <div className={`pf-board ${className}`}>
      <ul className={`grid gap-5 sm:gap-6 ${columns}`} data-testid={testId}>
        {pictures.map((picture, i) => <Polaroid key={picture.key} picture={picture} index={i} dropping={dropping} />)}
        {children}
      </ul>
    </div>
  );
}
