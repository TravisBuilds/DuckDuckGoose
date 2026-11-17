import type { CSSProperties, ReactNode } from 'react';

const TILTS = [-3, 2, -1, 3, -2, 4, -4, 1];
const PINS = ['var(--coral)', 'var(--blue)', 'var(--green)', 'var(--mustard)', 'var(--aqua)', 'var(--lilac)'];

export interface PinnedPicture {
  key: string;
  src: string;
  alt: string;
  href?: string;
  caption?: string | null;
  note?: string | null;
}

/** One pinned picture: white border, a pin, a slight tilt; drops in (staggered) when `dropping`. `children` sit under it. */
export function Polaroid({ picture, index = 0, dropping = false, children }: {
  picture: PinnedPicture; index?: number; dropping?: boolean; children?: ReactNode;
}) {
  const tilt = TILTS[index % TILTS.length];
  const style = { transform: `rotate(${tilt}deg)`, ['--pf-tilt' as string]: `${tilt}deg`, animationDelay: dropping ? `${index * 80}ms` : undefined } as CSSProperties;
  const img = <img src={picture.src} alt={picture.alt} loading="lazy" className="aspect-[4/3] w-full rounded-[3px] bg-[#F2EEE5] object-cover" />;
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

/** The cork board: pictures pinned in a grid, with room for sticky notes after them. */
export default function PinBoard({ pictures, dropping = false, columns = 'grid-cols-2 sm:grid-cols-4', testId, children, className = '' }: {
  pictures: PinnedPicture[]; dropping?: boolean; columns?: string; testId?: string; children?: ReactNode; className?: string;
}) {
  return (
    <div className={`pf-cork ${className}`}>
      <ul className={`grid gap-5 sm:gap-6 ${columns}`} data-testid={testId}>
        {pictures.map((picture, i) => <Polaroid key={picture.key} picture={picture} index={i} dropping={dropping} />)}
        {children}
      </ul>
    </div>
  );
}
