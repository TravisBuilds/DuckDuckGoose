import type { ReactNode } from 'react';
import type { CastColour } from '@/lib/castColour.mjs';
import { BeanArt } from './art';
import { CastDot, bandStyle, hueOf } from './castTheme';
import Seal, { type SealState } from './Seal';

/**
 * A character as a card (DESIGN-TARGETS-v2 §6.2): a band in their cast colour with their figure and
 * seal, then the name with the colour named in words, then whatever the cast panel puts inside
 * (description, outfits, pictures, voice, lock). A card that needs you gets a mustard ring.
 */
export default function CharacterCard({ colour, index = 0, picture, seal, needsYou = false, title, testId, children }: {
  colour: CastColour;
  index?: number;
  picture?: { src: string; alt: string } | null;
  seal: { state: SealState; top: string; bottom: string; testId?: string };
  needsYou?: boolean;
  title: ReactNode;
  testId?: string;
  children?: ReactNode;
}) {
  const hue = hueOf(colour);
  return (
    <div data-testid={testId} data-colour={colour.colour}
      className={`pf-card relative min-w-0 ${needsYou ? 'shadow-[0_0_0_3px_var(--mustard),var(--shadow-1)]' : ''}`}>
      <div className="relative h-[132px] rounded-t-card" style={bandStyle(colour)}>
        {picture ? (
          <img src={picture.src} alt={picture.alt} loading="lazy"
            className="absolute bottom-0 left-1/2 h-[120px] w-[104px] -translate-x-1/2 rounded-t-[18px] border-4 border-b-0 border-white object-cover" />
        ) : (
          <BeanArt variant={index % 2 === 0 ? 1 : 2} className={`absolute -bottom-1.5 left-1/2 h-[140px] w-[110px] -translate-x-1/2 ${hue.fig}`} />
        )}
        <Seal state={seal.state} top={seal.top} bottom={seal.bottom} size={54} testId={seal.testId} className="!absolute right-3 top-3" />
      </div>
      <div className="px-[18px] pb-[18px] pt-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">{title}</div>
          <span className="inline-flex items-center gap-1.5 text-[12px] font-extrabold">
            <CastDot colour={colour} />{colour.narrator ? 'Narrator' : hue.name}{colour.patterned ? ' (striped)' : ''}
          </span>
        </div>
        {children}
      </div>
    </div>
  );
}
