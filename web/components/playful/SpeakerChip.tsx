import type { CastColour } from '@/lib/castColour.mjs';
import { CastDot, hueOf } from './castTheme';

/** A script speaker: soft fill, readable text and the bright dot of their cast colour. The narrator is ink. */
export default function SpeakerChip({ name, colour, testId, className = '' }: {
  name: string | null | undefined; colour: CastColour; testId?: string; className?: string;
}) {
  const hue = hueOf(colour);
  return (
    <span data-testid={testId} data-colour={colour.colour}
      className={`inline-flex max-w-full items-center gap-1.5 rounded-full px-2.5 py-1 text-[13px] font-extrabold leading-tight ${hue.soft} ${hue.ink} ${className}`}>
      {!colour.narrator && <CastDot colour={colour} />}
      <span className="truncate">{name || 'Narrator'}</span>
    </span>
  );
}
