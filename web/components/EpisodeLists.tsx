'use client';

import Link from 'next/link';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { getPresetById } from '@/lib/presets';
import { ApiError, API_URL, apiFetch, postJson } from '@/lib/studio-api';
import { formatLocalDate } from '@/lib/time';
import { ownerLabel } from '@/lib/labels';
import { cardStatus } from '@/lib/next-step.mjs';
import type { RunStateLike, StoryLike } from '@/lib/next-step.mjs';
import { posterShot, primaryVideo } from '@/lib/world.mjs';
import type { UniverseCard } from '@/lib/world.mjs';
import { byNeedsYou } from '@/lib/journey.mjs';
import { chip, input as field, tones, type Tone } from '@/lib/ui';
import { Icon } from './playful';

/**
 * The episode lists of the home and the Episodes page (DESIGN-TARGETS-v2 §6.1): Keep going (a
 * decision waiting for you first), Recently finished (playing right here) and the episodes in no
 * Universe, each with a way to add it to one. Each status costs a run-state read, so only the
 * first cards get one.
 */

export interface Episode {
  episode_id: string;
  live_mode: boolean;
  g108_approved: boolean;
  created_at: string | null;
  user_id: string | null;
  owner_name: string | null;
}

type EpisodeState = RunStateLike & {
  archetype_id?: string | null;
  shot_outcomes?: Record<string, { still_url?: string | null }>;
};
type StoryShape = StoryLike & { story_shape?: { logline?: string } | null };

export interface Summary {
  state: EpisodeState | null; // null: the video has not started
  story: StoryShape | null;
  logline: string | null;
  style: string | null;
}

export const STATUS_LIMIT = 20;
const PARALLEL_READS = 4;
export const UNREACHABLE = 'The studio could not be reached. Check your connection and reload.';

async function loadSummary(id: string): Promise<Summary> {
  let state: EpisodeState;
  try {
    state = await apiFetch<EpisodeState>(`/api/episodes/${id}`);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return { state: null, story: null, logline: null, style: null };
    throw e;
  }
  // the run state carries no story outline: read it for the logline and the story step
  const story = await apiFetch<StoryShape>(`/api/episodes/${id}/story-shape`).catch(() => null);
  return {
    state,
    story,
    logline: story?.story_shape?.logline || null,
    style: state.archetype_id ? getPresetById(state.archetype_id)?.name ?? null : null,
  };
}

/** The first STATUS_LIMIT episodes' summaries, read a few at a time; each id once. */
export function useSummaries(shown: Episode[]) {
  const [summaries, setSummaries] = useState<Record<string, Summary | null>>({});
  const requested = useRef(new Set<string>());
  useEffect(() => {
    const todo = shown.slice(0, STATUS_LIMIT).map((ep) => ep.episode_id).filter((id) => !requested.current.has(id));
    if (todo.length === 0) return;
    todo.forEach((id) => requested.current.add(id));
    let next = 0;
    const worker = async () => {
      while (next < todo.length) {
        const id = todo[next++];
        const summary = await loadSummary(id).catch(() => null);
        setSummaries((prev) => ({ ...prev, [id]: summary }));
      }
    };
    Array.from({ length: Math.min(PARALLEL_READS, todo.length) }, worker);
  }, [shown]);
  return summaries;
}

function episodeUrl(id: string, file: string): string {
  return `${API_URL}/api/episodes/${encodeURIComponent(id)}/files/${file}`;
}

/** A remote still as-is; a local one (dry-run stand-in, pre-approved copy) through the API. */
function posterUrl(id: string, shotId: string, stillUrl: string): string {
  if (/^https?:\/\//.test(stillUrl)) return stillUrl;
  return `${API_URL}/api/episodes/${encodeURIComponent(id)}/shots/${encodeURIComponent(shotId)}/still`;
}

function posterOf(id: string, summary: Summary | null | undefined): string | undefined {
  const outcomes = summary?.state?.shot_outcomes ?? null;
  const shot = posterShot(outcomes);
  return shot && outcomes ? posterUrl(id, shot, outcomes[shot].still_url ?? '') : undefined;
}

/** The card's status chip. A step only its owner (or an admin) can take says who it waits on. */
export function chipFor(summary: Summary, episode: Episode, myUserId: string | null, isAdmin: boolean): { label: string; tone: Tone } {
  const mine = episode.user_id === null || episode.user_id === myUserId;
  return cardStatus(summary.state, summary.story, { mine, canAct: isAdmin || mine, ownerName: episode.owner_name });
}

const THUMBS = ['#E3F2FD,#9FD3F7', '#F3FCD6,#D5F76A', '#FFF7D1,#FFE36B', '#E8F8FE,#BDEBFA'];

/** A list row's round thumbnail: the episode's first still, or a soft gradient circle with a film icon. */
function Thumb({ id, poster, index }: { id: string; poster?: string; index: number }) {
  return (
    <div aria-hidden="true" className="relative grid size-16 flex-none place-items-center overflow-hidden rounded-full text-ink shadow-card"
      style={{ background: `linear-gradient(145deg,${THUMBS[index % THUMBS.length]})` }}>
      {poster ? <img src={poster} alt="" loading="lazy" className="h-full w-full object-cover" /> : <Icon name="film" size={24} />}
      <span className="sr-only">{id}</span>
    </div>
  );
}

function Meta({ episode, universe, myUserId, isAdmin }: {
  episode: Episode; universe: UniverseCard | null; myUserId: string | null; isAdmin: boolean;
}) {
  const id = episode.episode_id;
  const owner = ownerLabel(episode.user_id, episode.owner_name, myUserId);
  const date = formatLocalDate(episode.created_at);
  return (
    <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12.5px] text-ink-soft">
      <span data-testid={`episode-owner-${id}`}>{owner === 'You' ? 'By you' : `By ${owner}`}</span>
      {universe && <span data-testid={`episode-universe-${id}`}>In {universe.name}</span>}
      {date && <span>{date}</span>}
      {episode.live_mode && <span className={`${chip} ${tones.warn}`}>Live mode</span>}
      {isAdmin && episode.g108_approved && <span className={`${chip} ${tones.ok}`}>G1.08 approved</span>}
    </p>
  );
}

export function EpisodeCard({ episode, summary, myUserId, isAdmin, universe, index = 0, children }: {
  episode: Episode; summary: Summary | null | undefined; myUserId: string | null; isAdmin: boolean;
  universe: UniverseCard | null; index?: number; children?: ReactNode;
}) {
  const id = episode.episode_id;
  const status = summary ? chipFor(summary, episode, myUserId, isAdmin) : null;
  const needsYou = status?.tone === 'todo';
  const title = summary?.logline || summary?.style || `Episode ${id}`;
  return (
    <li className={`pf-card relative min-w-0 ${needsYou ? 'shadow-[0_0_0_3px_var(--mustard),var(--shadow-1)]' : ''}`}>
      <div className="flex items-center gap-3.5 py-3 pl-3 pr-3.5">
        <Thumb id={id} poster={posterOf(id, summary)} index={index} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[11.5px] font-extrabold uppercase tracking-[0.04em] text-ink-soft">
            {universe ? universe.name : 'Not in a Universe'} · <span className="font-mono normal-case">{id}</span>
            {summary?.style && summary.logline ? ` · ${summary.style}` : ''}
          </p>
          <h3 className="line-clamp-2 break-words text-[16px]">
            <Link href={`/studio/episodes/${id}`} data-testid={`episode-card-${id}`}
              className="after:absolute after:inset-0 after:rounded-card after:content-['']">
              {title}
            </Link>
          </h3>
          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            {status && <span className={`${chip} ${tones[status.tone]}`} data-testid={`episode-status-${id}`}>{status.label}</span>}
          </div>
          <Meta episode={episode} universe={universe} myUserId={myUserId} isAdmin={isAdmin} />
        </div>
        {needsYou && (
          <Link href={`/studio/episodes/${id}`} className="pf-btn pf-btn-primary pf-btn-sm relative z-10 hidden sm:inline-flex">
            Review
          </Link>
        )}
        <span aria-hidden="true" className={`pf-round is-solid ${needsYou ? 'sm:hidden' : ''}`}><Icon name="triangle" size={16} /></span>
      </div>
      {children && <div className="relative z-10">{children}</div>}
    </li>
  );
}

/** A finished episode: its video plays right here (nothing loads until play), with its download. */
export function FinishedCard({ episode, summary, myUserId, isAdmin, universe, children }: {
  episode: Episode; summary: Summary | null | undefined; myUserId: string | null; isAdmin: boolean;
  universe: UniverseCard | null; children?: ReactNode;
}) {
  const id = episode.episode_id;
  const [primary, setPrimary] = useState<string | null | undefined>(undefined);
  useEffect(() => {
    let alive = true;
    apiFetch<{ files: Record<string, { available: boolean }> }>(`/api/episodes/${id}/files`)
      .then((r) => alive && setPrimary(primaryVideo(r.files)))
      .catch(() => alive && setPrimary(null));
    return () => {
      alive = false;
    };
  }, [id]);
  const status = summary ? chipFor(summary, episode, myUserId, isAdmin) : null;
  return (
    <li className="pf-card min-w-0 overflow-hidden" data-testid={`finished-card-${id}`}>
      {primary ? (
        <video controls playsInline preload="none" poster={posterOf(id, summary)} src={episodeUrl(id, primary)}
          className="h-56 w-full bg-ink object-contain" data-testid={`finished-video-${id}`} />
      ) : (
        <div className="flex h-56 w-full items-center justify-center bg-mist p-4 text-center text-[13px] text-ink-soft">
          {primary === undefined ? 'Loading the video…' : 'No video file is left for this episode.'}
        </div>
      )}
      <div className="p-4">
        <p className="truncate text-[11.5px] font-extrabold uppercase tracking-[0.04em] text-ink-soft">
          {universe ? universe.name : 'Not in a Universe'}
        </p>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Link href={`/studio/episodes/${id}`} data-testid={`episode-card-${id}`}
            className="min-w-0 break-words font-display text-[16px] font-semibold">
            {summary?.logline || <span className="font-mono">{id}</span>}
          </Link>
          {status && (
            <span className={`${chip} ${tones[status.tone]}`} data-testid={`episode-status-${id}`}>
              {status.tone === 'ok' && <Icon name="check" size={11} />}{status.label}
            </span>
          )}
        </div>
        <Meta episode={episode} universe={universe} myUserId={myUserId} isAdmin={isAdmin} />
        {primary && (
          <a href={`${episodeUrl(id, primary)}?download=true`} className="pf-btn pf-btn-quiet pf-btn-sm mt-3 w-full"
            data-testid={`finished-download-${id}`}>
            Download
          </a>
        )}
      </div>
      {children}
    </li>
  );
}

/** Put an episode in one of the reader's Universes (POST /api/episodes/{id}/series); a refusal shows as the API says it. */
export function AddToUniverse({ episodeId, choices, onAdded }: {
  episodeId: string; choices: { series_id: string; name: string }[]; onAdded: () => void;
}) {
  const [choice, setChoice] = useState('');
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const picked = choice || choices[0]?.series_id || '';
  if (choices.length === 0) {
    return (
      <p className="border-t border-dashed border-line px-4 py-3 text-[13px] text-ink-soft">
        Not in a Universe.{' '}
        <Link href="/studio/universes/new" className="pf-link-text" data-testid={`add-to-universe-create-${episodeId}`}>
          Create a Universe
        </Link>{' '}
        to keep its characters and places for the next episodes.
      </p>
    );
  }
  const add = async () => {
    setBusy(true);
    setProblem(null);
    try {
      await postJson(`/api/episodes/${episodeId}/series`, { series_id: picked });
      onAdded();
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : UNREACHABLE);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="border-t border-dashed border-line px-4 py-3" data-testid={`add-to-universe-${episodeId}`}>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <label htmlFor={`add-to-universe-pick-${episodeId}`} className="text-[13px] font-extrabold">Add to a Universe</label>
        {choices.length > 1 && (
          <select id={`add-to-universe-pick-${episodeId}`} value={picked} onChange={(e) => setChoice(e.target.value)}
            className={`${field} sm:w-auto`} data-testid={`add-to-universe-pick-${episodeId}`}>
            {choices.map((c) => <option key={c.series_id} value={c.series_id}>{c.name}</option>)}
          </select>
        )}
        <button type="button" onClick={add} disabled={busy || !picked} className="pf-btn pf-btn-peach pf-btn-sm"
          data-testid={`add-to-universe-button-${episodeId}`}>
          {busy ? 'Adding…' : choices.length > 1 ? 'Add' : `Add to ${choices[0].name}`}
        </button>
      </div>
      {problem && (
        <p className="mt-2 text-[13px] text-coral-ink" role="alert" data-testid={`add-to-universe-problem-${episodeId}`}>
          {problem}
        </p>
      )}
    </div>
  );
}

/**
 * Mine / Everyone for the episode lists (members start with their own, admins with everyone's). A
 * member's second list holds only the episodes they may open, so for them it reads All.
 */
export function ScopeToggle({ scope, onChange, isAdmin }: {
  scope: 'mine' | 'everyone';
  onChange: (scope: 'mine' | 'everyone') => void;
  isAdmin: boolean;
}) {
  return (
    <div role="group" aria-label="Show videos" className="inline-flex gap-1 rounded-full bg-white p-1 shadow-card">
      {(['mine', 'everyone'] as const).map((value) => (
        <button key={value} type="button" aria-pressed={scope === value} onClick={() => onChange(value)}
          data-testid={`scope-${value}`}
          className={`min-h-9 rounded-full px-4 text-[13.5px] font-extrabold ${scope === value ? 'bg-blue-deep text-white' : 'text-ink-soft hover:bg-page'}`}>
          {value === 'mine' ? 'Mine' : isAdmin ? 'Everyone' : 'All'}
        </button>
      ))}
    </div>
  );
}

export interface Sections {
  continue: { episode: Episode; universe: UniverseCard | null }[];
  finished: { episode: Episode; universe: UniverseCard | null }[];
  unfiled: { episode: Episode; universe: UniverseCard | null }[];
}

/** The three lists, in the order Keep going, Recently finished, Not in a Universe. */
export function EpisodeSections({ sections, summaries, myUserId, isAdmin, choices, mayChange, onAdded, finishedLimit, header }: {
  sections: Sections;
  summaries: Record<string, Summary | null>;
  myUserId: string | null;
  isAdmin: boolean;
  choices: { series_id: string; name: string }[];
  mayChange: (episode: Episode) => boolean;
  onAdded: () => void;
  finishedLimit: number | null;
  header?: ReactNode;
}) {
  const [allFinished, setAllFinished] = useState(false);
  const toneOf = (entry: { episode: Episode }) => {
    const summary = summaries[entry.episode.episode_id];
    return summary ? chipFor(summary, entry.episode, myUserId, isAdmin).tone : null;
  };
  const keepGoing = byNeedsYou(sections.continue, toneOf);
  const finishedShown = allFinished || finishedLimit === null ? sections.finished : sections.finished.slice(0, finishedLimit);
  return (
    <div data-testid="episode-list">
      {header}
      {keepGoing.length > 0 && (
        <section className="mt-6" data-testid="continue-section" aria-labelledby="continue-title">
          <h3 id="continue-title" className="mb-1 text-[20px]">Keep going</h3>
          <p className="mb-3 text-[13px] text-ink-soft">Episodes on their way, the ones waiting for you first.</p>
          <ul className="grid gap-4 lg:grid-cols-2">
            {keepGoing.map(({ episode, universe }, i) => (
              <EpisodeCard key={episode.episode_id} episode={episode} summary={summaries[episode.episode_id]}
                myUserId={myUserId} isAdmin={isAdmin} universe={universe} index={i} />
            ))}
          </ul>
        </section>
      )}
      {sections.finished.length > 0 && (
        <section className="mt-8" data-testid="finished-section" aria-labelledby="finished-title">
          <h3 id="finished-title" className="mb-1 text-[20px]">Recently finished</h3>
          <p className="mb-3 text-[13px] text-ink-soft">Play them here or download them.</p>
          <ul className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
            {finishedShown.map(({ episode, universe }) => (
              <FinishedCard key={episode.episode_id} episode={episode} summary={summaries[episode.episode_id]}
                myUserId={myUserId} isAdmin={isAdmin} universe={universe}>
                {!universe && mayChange(episode) && (
                  <AddToUniverse episodeId={episode.episode_id} choices={choices} onAdded={onAdded} />
                )}
              </FinishedCard>
            ))}
          </ul>
          {sections.finished.length > finishedShown.length && (
            <button type="button" className="pf-btn pf-btn-quiet mt-4" onClick={() => setAllFinished(true)}>
              Show {sections.finished.length - finishedShown.length} more
            </button>
          )}
        </section>
      )}
      {sections.unfiled.length > 0 && (
        <section className="mt-8 scroll-mt-28" id="not-in-a-universe" data-testid="unfiled-section" aria-labelledby="unfiled-title">
          <h3 id="unfiled-title" className="mb-1 text-[20px]">Not in a Universe</h3>
          <p className="mb-3 text-[13px] text-ink-soft">
            Older episodes. Add one to a Universe to reuse its cast and places in the next episodes.
          </p>
          <ul className="grid gap-3">
            {sections.unfiled.map(({ episode }, i) => (
              <EpisodeCard key={episode.episode_id} episode={episode} summary={summaries[episode.episode_id]}
                myUserId={myUserId} isAdmin={isAdmin} universe={null} index={i + 1}>
                <AddToUniverse episodeId={episode.episode_id} choices={choices} onAdded={onAdded} />
              </EpisodeCard>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
