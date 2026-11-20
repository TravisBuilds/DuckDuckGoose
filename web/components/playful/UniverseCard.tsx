import Link from 'next/link';
import type { UniverseCard as UniverseCardData } from '@/lib/world.mjs';
import { API_URL } from '@/lib/studio-api';
import { countsLine, healthLine } from '@/lib/consistency.mjs';
import { CAST_COLOURS } from '@/lib/castColour.mjs';
import { CREATION_NAMES, homeJourney } from '@/lib/journey.mjs';
import { ownerLabel } from '@/lib/labels';
import { bandStyle } from './castTheme';
import NextStepPill from './NextStepPill';

const COVERS = ['#E3F2FD,#A9DAF8', '#E8F8FE,#BDEBFA', '#F3FCD6,#DDF78A', '#FFF7D1,#FFE680', '#FFEDE5,#FFC4A8', '#F2EEFE,#D5C9FC'];
const CIRCLES = ['var(--green)', 'var(--mustard)', 'var(--blue)', 'var(--aqua)', 'var(--green)', 'var(--aqua)'];

function coverIndex(id: string): number {
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h % COVERS.length;
}

/**
 * A Universe on the home: its cover, name and one-line idea, the five-step progress, what it keeps,
 * and a "Next" pill to the step that waits. Without a mood board yet, the
 * cover is a soft gradient with a big accent circle and one soft circle per character (up to three).
 * The whole card opens the Universe; the pill opens the step.
 */
export default function UniverseCard({ world, myUserId }: { world: UniverseCardData; myUserId: string | null }) {
  const id = world.series_id;
  const counts = world.counts;
  const journey = homeJourney(world);
  const owner = ownerLabel(world.owner_user_id, world.owner_name, myUserId);
  const href = `/studio/universes/${encodeURIComponent(id)}`;
  const nextHref = journey.next.step ? `${href}/create?step=${journey.next.step}`
    : journey.steps ? `/studio/create?world=${encodeURIComponent(id)}` : href;
  const healthTone = counts.characters === 0 ? 'bg-mist text-ink-soft'
    : counts.locked_characters >= counts.characters ? 'bg-green-soft text-green-ink' : 'bg-mustard-soft text-mustard-ink';
  const cast = Math.min(3, counts.characters);
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
              <span className="absolute -bottom-16 -right-10 size-48 rounded-full" style={{ background: CIRCLES[art] }} />
              <span className="absolute -left-8 -top-10 size-28 rounded-full bg-white/50" />
              <span className="absolute bottom-4 left-5 flex -space-x-3">
                {Array.from({ length: cast }, (_, i) => (
                  <span key={i} className="size-12 rounded-full border-[3px] border-white shadow-card"
                    style={bandStyle({ colour: CAST_COLOURS[i], patterned: false, index: i, narrator: false })} />
                ))}
              </span>
            </div>
          )}
        </div>
        <div className="flex flex-1 flex-col px-5 pb-5 pt-4">
          <h3 className="break-words text-[18px]">
            <Link href={href} className="after:absolute after:inset-0 after:rounded-card after:content-['']">{world.name}</Link>
          </h3>
          {world.profile.logline && <p className="mt-0.5 line-clamp-2 break-words text-[13px] text-ink-soft">{world.profile.logline}</p>}
          {journey.steps && (
            <div className="mt-3.5 mb-1.5 flex gap-1" aria-hidden="true">
              {journey.steps.map((s) => (
                <i key={s.key} className={`h-[6px] flex-1 rounded-full ${s.state === 'done' ? 'bg-green' : s.state === 'now' ? 'bg-blue' : 'bg-line'}`} />
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
