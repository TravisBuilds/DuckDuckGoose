import Link from 'next/link';
import type { UniverseCard as UniverseCardData } from '@/lib/world.mjs';
import { API_URL } from '@/lib/studio-api';
import { countsLine, healthLine } from '@/lib/consistency.mjs';
import { CAST_COLOURS } from '@/lib/castColour.mjs';
import { CREATION_NAMES, homeJourney, sealFor } from '@/lib/journey.mjs';
import { ownerLabel } from '@/lib/labels';
import { BeanArt, BoatArt, KilnArt } from './art';
import { HUES } from './castTheme';
import NextStepPill from './NextStepPill';
import Seal from './Seal';

const COVERS = ['#FCD9C4,#F6B48C', '#CBEAF0,#8FCAD6', '#E1ECF8,#9DBEE6', '#EEE8F9,#C9B8EA', '#E3F3E4,#A9D9AB', '#FBF1D6,#EBD49A'];

function coverIndex(id: string): number {
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h % COVERS.length;
}

/**
 * A Universe on the home (DESIGN-TARGETS-v2 §6.1): its cover with the consistency seal, name and
 * one-line idea, the five-step progress, what it keeps, and a "Next" pill to the step that waits.
 * The whole card opens the Universe; the pill opens the step.
 */
export default function UniverseCard({ world, myUserId }: { world: UniverseCardData; myUserId: string | null }) {
  const id = world.series_id;
  const counts = world.counts;
  const journey = homeJourney(world);
  const seal = sealFor(counts);
  const owner = ownerLabel(world.owner_user_id, world.owner_name, myUserId);
  const href = `/studio/universes/${encodeURIComponent(id)}`;
  const nextHref = journey.next.step ? `${href}/create?step=${journey.next.step}`
    : journey.steps ? `/studio/create?world=${encodeURIComponent(id)}` : href;
  const healthTone = counts.characters === 0 ? 'bg-[#EFEDE7] text-ink-soft'
    : counts.locked_characters >= counts.characters ? 'bg-green-soft text-green-ink' : 'bg-mustard-soft text-mustard-ink';
  const beans = Math.min(3, counts.characters);
  const art = coverIndex(id);
  return (
    <li className="min-w-0">
      <article className="pf-card pf-lift relative flex h-full flex-col" data-testid={`universe-card-${id}`}>
        <div className="relative h-44 overflow-hidden rounded-t-card"
          style={world.cover ? undefined : { background: `linear-gradient(160deg,${COVERS[art]})` }}>
          {world.cover ? (
            <img src={`${API_URL}${world.cover.thumb_url}`} alt="" loading="lazy" className="h-full w-full object-cover"
              data-testid={`universe-cover-${id}`} />
          ) : (
            <div aria-hidden="true" className="absolute inset-0">
              {art % 2 === 0
                ? <KilnArt className="absolute bottom-2.5 left-5 h-[100px] w-[120px]" />
                : <BoatArt className="absolute bottom-3.5 left-8 h-[100px] w-[124px]" />}
              {Array.from({ length: beans }, (_, i) => (
                <BeanArt key={i} variant={i % 2 === 0 ? 1 : 2}
                  className={`absolute bottom-1.5 h-[88px] w-[70px] ${HUES[CAST_COLOURS[i]].fig}`} style={{ left: 150 + i * 64 }} />
              ))}
            </div>
          )}
          <Seal state={seal.state} top={seal.top} bottom={seal.bottom} className="!absolute right-4 top-3.5" />
        </div>
        <div className="flex flex-1 flex-col px-5 pb-5 pt-4">
          <h3 className="break-words text-[19px]">
            <Link href={href} className="after:absolute after:inset-0 after:rounded-card after:content-['']">{world.name}</Link>
          </h3>
          {world.profile.logline && <p className="mt-0.5 line-clamp-2 break-words text-[13px] text-ink-soft">{world.profile.logline}</p>}
          {journey.steps && (
            <div className="mt-3.5 mb-1.5 flex gap-1" aria-hidden="true">
              {journey.steps.map((s) => (
                <i key={s.key} className={`h-[7px] flex-1 rounded-full ${s.state === 'done' ? 'bg-green' : s.state === 'now' ? 'bg-mustard' : 'bg-[#EAE5DA]'}`} />
              ))}
            </div>
          )}
          <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-[12px] text-ink-soft">
            {journey.steps && (
              <span className="font-bold">
                {journey.current ? `Step ${journey.step} of 5 · ${CREATION_NAMES[journey.current]}` : 'All 5 steps done'}
              </span>
            )}
            <span data-testid={`universe-counts-${id}`}>{countsLine(counts)}</span>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-[12px] text-ink-soft">
            <span className={`pf-chip ${healthTone}`} data-testid={`universe-health-${id}`}>{healthLine(counts)}</span>
            <span>{owner === 'You' ? 'By you' : `By ${owner}`}</span>
          </div>
          <div className="mt-auto pt-3.5">
            <NextStepPill text={journey.next.text} tone={journey.next.tone} href={nextHref} testId={`universe-next-${id}`} />
          </div>
        </div>
      </article>
    </li>
  );
}
