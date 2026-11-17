import type { ReactNode } from 'react';

export type RefTag = 'character' | 'place' | 'prop' | 'feeling';

const TAGS: Record<RefTag, { label: string; chip: string }> = {
  character: { label: 'Character', chip: 'pf-chip-bad' },
  place: { label: 'Place', chip: 'pf-chip-blue' },
  prop: { label: 'Prop', chip: 'pf-chip-lock' },
  feeling: { label: 'Feeling', chip: 'pf-chip-warn' },
};

/** A reference photo tile: the square picture, an optional tag chip and room for its actions. */
export default function RefTile({ src, alt, href, tag, caption, testId, children }: {
  src: string; alt: string; href?: string; tag?: RefTag | null; caption?: string | null; testId?: string; children?: ReactNode;
}) {
  const img = <img src={src} alt={alt} loading="lazy" className="aspect-square w-full bg-[#F2EEE5] object-cover" />;
  return (
    <li className="min-w-0 overflow-hidden rounded-[16px] bg-white shadow-card" data-testid={testId}>
      {href ? <a href={href} target="_blank" rel="noreferrer" className="block" title="Open the full picture">{img}</a> : img}
      <div className="flex flex-col gap-1 p-1.5">
        {tag && <span className={`pf-chip ${TAGS[tag].chip} self-start !px-2 !py-0.5 !text-[11px]`}>{TAGS[tag].label}</span>}
        {caption && <p className="break-words px-1 text-[12px] font-semibold leading-snug text-ink-soft">{caption}</p>}
        {children}
      </div>
    </li>
  );
}
