import type { ReactNode } from 'react';
import Icon from './Icon';

/**
 * A place or prop as a card: its main photo (or a line icon on a soft tile until it has one), its
 * name, a Place/Prop chip and how many photos it keeps, then whatever the panel puts inside.
 */
export default function ThingCard({ kind, index = 0, picture, title, photos, testId, children }: {
  kind: 'place' | 'prop';
  index?: number;
  picture?: { src: string; alt: string; href?: string } | null;
  title: ReactNode;
  photos: number;
  testId?: string;
  children?: ReactNode;
}) {
  const soft = ['bg-blue-soft', 'bg-aqua-soft', 'bg-green-soft', 'bg-mustard-soft', 'bg-coral-soft', 'bg-lilac-soft'][index % 6];
  return (
    <div className="pf-card min-w-0 p-3.5" data-testid={testId}>
      <div className={`relative grid h-[96px] place-items-center overflow-hidden rounded-[16px] ${picture ? 'bg-mist' : soft}`}>
        {picture ? (
          picture.href ? (
            <a href={picture.href} target="_blank" rel="noreferrer" title="Open the full photo" className="block h-full w-full">
              <img src={picture.src} alt={picture.alt} loading="lazy" className="h-full w-full object-cover" />
            </a>
          ) : <img src={picture.src} alt={picture.alt} loading="lazy" className="h-full w-full object-cover" />
        ) : (
          <span aria-hidden="true" className="mb-4 grid size-12 place-items-center rounded-full bg-white text-blue-ink shadow-card">
            <Icon name={kind === 'prop' ? 'box' : 'place'} size={24} />
          </span>
        )}
        {!picture && (
          <span className="absolute bottom-1.5 left-1/2 -translate-x-1/2 whitespace-nowrap rounded-full bg-white/90 px-2 text-[11px] font-extrabold text-ink-soft">
            No photo yet
          </span>
        )}
      </div>
      <div className="mt-2.5 text-center">
        {title}
        <div className="mt-1.5 flex flex-wrap justify-center gap-1.5">
          <span className={`pf-chip ${kind === 'place' ? 'pf-chip-blue' : 'pf-chip-lilac'}`}>{kind === 'place' ? 'Place' : 'Prop'}</span>
          <span className={`pf-chip ${photos > 0 ? 'pf-chip-lock' : 'pf-chip-open'}`}>{photos === 1 ? '1 photo' : `${photos} photos`}</span>
        </div>
      </div>
      {children}
    </div>
  );
}
