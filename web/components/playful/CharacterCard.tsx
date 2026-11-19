import type { ReactNode } from 'react';
import type { CastColour } from '@/lib/castColour.mjs';
import Avatar from './Avatar';
import { CastDot, bandStyle, hueOf } from './castTheme';

/**
 * A character as a card: a band in their cast colour's soft gradient with their main picture (or
 * their initials in a circle until they have one), then the name with the colour named in
 * words, then whatever the cast panel puts inside (description, outfits, pictures, voice, lock). A
 * card that needs you gets a yellow ring.
 */
export default function CharacterCard({ colour, name, picture, needsYou = false, title, testId, children }: {
  colour: CastColour;
  name: string;
  picture?: { src: string; alt: string } | null;
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
          <span className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full bg-white/70 p-1.5 shadow-card">
            <Avatar name={name} hue={colour.colour} size={84} />
          </span>
        )}
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
