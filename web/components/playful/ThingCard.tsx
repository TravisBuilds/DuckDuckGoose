import type { ReactNode } from 'react';
import { BellArt, BoatArt, KilnArt } from './art';

/**
 * A place or prop as a card: its main photo (or a little drawing until it has one) on a soft tile,
 * its name, a Place/Prop chip and how many photos it keeps, then whatever the panel puts inside.
 */
export default function ThingCard({ kind, index = 0, picture, title, photos, testId, children }: {
  kind: 'place' | 'prop';
  index?: number;
  picture?: { src: string; alt: string } | null;
  title: ReactNode;
  photos: number;
  testId?: string;
  children?: ReactNode;
}) {
  const soft = ['bg-coral-soft', 'bg-blue-soft', 'bg-mustard-soft', 'bg-aqua-soft', 'bg-green-soft', 'bg-lilac-soft'][index % 6];
  return (
    <div className="pf-card min-w-0 p-3.5" data-testid={testId}>
      <div className={`grid h-[96px] place-items-center overflow-hidden rounded-[14px] ${picture ? 'bg-[#F2EEE5]' : soft}`}>
        {picture ? <img src={picture.src} alt={picture.alt} loading="lazy" className="h-full w-full object-cover" />
          : kind === 'prop' ? <BellArt className="h-[60px] w-[60px]" />
            : index % 2 === 0 ? <KilnArt className="h-[66px] w-[80px]" /> : <BoatArt className="h-[66px] w-[80px]" />}
      </div>
      <div className="mt-2.5 text-center">
        {title}
        <div className="mt-1.5 flex flex-wrap justify-center gap-1.5">
          <span className={`pf-chip ${kind === 'place' ? 'pf-chip-blue' : 'pf-chip-lock'}`}>{kind === 'place' ? 'Place' : 'Prop'}</span>
          <span className={`pf-chip ${photos > 0 ? 'pf-chip-lock' : 'pf-chip-open'}`}>{photos === 1 ? '1 photo' : `${photos} photos`}</span>
        </div>
      </div>
      {children}
    </div>
  );
}
