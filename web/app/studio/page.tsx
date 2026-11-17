'use client';

import Link from 'next/link';
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useMe } from '@/components/StudioShell';
import { UniverseCreateForm } from '@/components/UniverseCreateForm';
import { getPresetById } from '@/lib/presets';
import { ApiError, API_URL, apiFetch, postJson } from '@/lib/studio-api';
import { formatLocalDate } from '@/lib/time';
import { ownerLabel } from '@/lib/labels';
import { cardStatus } from '@/lib/next-step.mjs';
import type { RunStateLike, StoryLike } from '@/lib/next-step.mjs';
import { countsLine, healthLine } from '@/lib/consistency.mjs';
import { homeSections, isFinished, isFirstRun, mayChange, posterShot, primaryVideo, universeChoices } from '@/lib/world.mjs';
import type { UniverseCard } from '@/lib/world.mjs';
import { btnPrimary, btnSecondary, card, chip, focusRing, input as field, link, tones, type Tone } from '@/lib/ui';

interface Episode {
  episode_id: string;
  live_mode: boolean;
  g108_approved: boolean;
  created_at: string | null;
  user_id: string | null;
  owner_name: string | null;
}

interface Summary {
  state: EpisodeState | null; // null: the video has not started
  story: StoryShape | null;
  logline: string | null;
  style: string | null;
}

interface Health {
  status?: string;
  temporal?: boolean;
}

type EpisodeState = RunStateLike & {
  archetype_id?: string | null;
  shot_outcomes?: Record<string, { still_url?: string | null }>;
};
type StoryShape = StoryLike & { story_shape?: { logline?: string } | null };

/** Each status chip costs a run-state read, so only the first cards get one. */
const STATUS_LIMIT = 20;
const PARALLEL_READS = 4;
/** Recently finished shows this many players at first. */
const FINISHED_SHOWN = 6;
const UNREACHABLE = 'The studio could not be reached. Check your connection and reload.';

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

function episodeUrl(id: string, file: string): string {
  return `${API_URL}/api/episodes/${encodeURIComponent(id)}/files/${file}`;
}

/** A remote still as-is; a local one (dry-run stand-in, pre-approved copy) through the API. */
function posterUrl(id: string, shotId: string, stillUrl: string): string {
  if (/^https?:\/\//.test(stillUrl)) return stillUrl;
  return `${API_URL}/api/episodes/${encodeURIComponent(id)}/shots/${encodeURIComponent(shotId)}/still`;
}

/** The card's status chip. A step only its owner (or an admin) can take says who it waits on. */
function chipFor(summary: Summary, episode: Episode, myUserId: string | null, isAdmin: boolean): { label: string; tone: Tone } {
  const mine = episode.user_id === null || episode.user_id === myUserId;
  return cardStatus(summary.state, summary.story, { mine, canAct: isAdmin || mine, ownerName: episode.owner_name });
}

function SectionHeading({ title, text, testId }: { title: string; text?: string; testId?: string }) {
  return (
    <div className="mb-3">
      <h2 className="text-lg font-semibold text-gray-900 dark:text-white" data-testid={testId}>{title}</h2>
      {text && <p className="text-sm text-gray-600 dark:text-gray-300">{text}</p>}
    </div>
  );
}

function UniverseTile({ world, myUserId }: { world: UniverseCard; myUserId: string | null }) {
  const counts = world.counts;
  const tone: Tone = counts.characters === 0 ? 'wait' : counts.locked_characters >= counts.characters ? 'ok' : 'warn';
  const owner = ownerLabel(world.owner_user_id, world.owner_name, myUserId);
  return (
    <li className="min-w-0">
      <Link href={`/studio/universes/${encodeURIComponent(world.series_id)}`} data-testid={`universe-card-${world.series_id}`}
        className={`flex h-full flex-col overflow-hidden rounded-lg border border-gray-200 bg-white transition-colors hover:border-blue-400 dark:border-gray-700 dark:bg-gray-800 ${focusRing}`}>
        {world.cover ? (
          <img src={`${API_URL}${world.cover.thumb_url}`} alt="" loading="lazy" className="h-32 w-full object-cover"
            data-testid={`universe-cover-${world.series_id}`} />
        ) : (
          <div aria-hidden="true" className="flex h-32 w-full items-center justify-center bg-gradient-to-br from-blue-100 to-purple-100 text-4xl font-bold text-blue-900 dark:from-blue-900/40 dark:to-purple-900/40 dark:text-blue-100">
            {world.name.trim().charAt(0).toUpperCase() || '?'}
          </div>
        )}
        <div className="flex flex-1 flex-col gap-1 p-4">
          <h3 className="break-words font-semibold text-gray-900 dark:text-white">{world.name}</h3>
          {world.profile.logline && (
            <p className="line-clamp-2 break-words text-sm text-gray-700 dark:text-gray-200">{world.profile.logline}</p>
          )}
          <p className="text-xs text-gray-600 dark:text-gray-300" data-testid={`universe-counts-${world.series_id}`}>{countsLine(counts)}</p>
          <p className="mt-auto pt-2">
            <span className={`${chip} ${tones[tone]}`} data-testid={`universe-health-${world.series_id}`}>{healthLine(counts)}</span>
          </p>
          <p className="text-xs text-gray-600 dark:text-gray-300">{owner === 'You' ? 'By you' : `By ${owner}`}</p>
        </div>
      </Link>
    </li>
  );
}

function EpisodeCard({ episode, summary, myUserId, isAdmin, universe, children }: {
  episode: Episode; summary: Summary | null | undefined; myUserId: string | null; isAdmin: boolean;
  universe: UniverseCard | null; children?: ReactNode;
}) {
  const date = formatLocalDate(episode.created_at);
  const owner = ownerLabel(episode.user_id, episode.owner_name, myUserId);
  const status = summary ? chipFor(summary, episode, myUserId, isAdmin) : null;
  return (
    <li className="rounded-lg border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
      <Link href={`/studio/episodes/${episode.episode_id}`} data-testid={`episode-card-${episode.episode_id}`}
        className={`block rounded-lg p-4 transition-colors hover:bg-blue-50/40 dark:hover:bg-gray-700 ${focusRing}`}>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="min-w-0 break-words font-semibold text-gray-900 dark:text-white">
            <span className="font-mono">{episode.episode_id}</span>
            {summary?.style && <span className="font-normal text-gray-700 dark:text-gray-200"> · {summary.style}</span>}
          </h3>
          {status && <span className={`${chip} ${tones[status.tone]}`} data-testid={`episode-status-${episode.episode_id}`}>{status.label}</span>}
        </div>
        {summary?.logline && <p className="mt-1 line-clamp-2 text-sm text-gray-700 dark:text-gray-200">{summary.logline}</p>}
        <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-gray-600 dark:text-gray-300">
          <span data-testid={`episode-owner-${episode.episode_id}`}>{owner === 'You' ? 'By you' : `By ${owner}`}</span>
          {universe && <span data-testid={`episode-universe-${episode.episode_id}`}>In {universe.name}</span>}
          {date && <span>{date}</span>}
          {episode.live_mode && <span className={`${chip} ${tones.warn}`}>Live mode</span>}
          {isAdmin && episode.g108_approved && <span className={`${chip} ${tones.ok}`}>G1.08 approved</span>}
        </p>
      </Link>
      {children}
    </li>
  );
}

/** A finished episode: its video plays right here (nothing loads until play), with its download. */
function FinishedCard({ episode, summary, myUserId, isAdmin, universe, children }: {
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
  const outcomes = summary?.state?.shot_outcomes ?? null;
  const shot = posterShot(outcomes);
  const poster = shot && outcomes ? posterUrl(id, shot, outcomes[shot].still_url ?? '') : undefined;
  const owner = ownerLabel(episode.user_id, episode.owner_name, myUserId);
  const status = summary ? chipFor(summary, episode, myUserId, isAdmin) : null;
  return (
    <li className="min-w-0 overflow-hidden rounded-lg border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800"
      data-testid={`finished-card-${id}`}>
      {primary ? (
        <video controls playsInline preload="none" poster={poster} src={episodeUrl(id, primary)}
          className="h-64 w-full bg-black object-contain" data-testid={`finished-video-${id}`} />
      ) : (
        <div className="flex h-64 w-full items-center justify-center bg-gray-100 p-4 text-center text-sm text-gray-700 dark:bg-gray-900 dark:text-gray-200">
          {primary === undefined ? 'Loading the video…' : 'No video file is left for this episode.'}
        </div>
      )}
      <div className="p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Link href={`/studio/episodes/${id}`} data-testid={`episode-card-${id}`}
            className={`min-w-0 break-words font-mono font-semibold text-gray-900 dark:text-white ${link}`}>
            {id}
          </Link>
          {status && <span className={`${chip} ${tones[status.tone]}`} data-testid={`episode-status-${id}`}>{status.label}</span>}
        </div>
        {summary?.logline && <p className="mt-1 line-clamp-2 text-sm text-gray-700 dark:text-gray-200">{summary.logline}</p>}
        <p className="mt-1 flex flex-wrap gap-x-3 text-sm text-gray-600 dark:text-gray-300">
          <span data-testid={`episode-owner-${id}`}>{owner === 'You' ? 'By you' : `By ${owner}`}</span>
          {universe && <span data-testid={`episode-universe-${id}`}>In {universe.name}</span>}
        </p>
        {primary && (
          <a href={`${episodeUrl(id, primary)}?download=true`} className={`${btnSecondary} mt-3 w-full`}
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
function AddToUniverse({ episodeId, choices, onAdded }: {
  episodeId: string; choices: { series_id: string; name: string }[]; onAdded: () => void;
}) {
  const [choice, setChoice] = useState('');
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const picked = choice || choices[0]?.series_id || '';
  if (choices.length === 0) {
    return (
      <p className="border-t border-gray-200 px-4 py-3 text-sm text-gray-700 dark:border-gray-700 dark:text-gray-200">
        Not in a Universe.{' '}
        <Link href="/studio/universes/new" className={link} data-testid={`add-to-universe-create-${episodeId}`}>
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
    <div className="border-t border-gray-200 px-4 py-3 dark:border-gray-700" data-testid={`add-to-universe-${episodeId}`}>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <label htmlFor={`add-to-universe-pick-${episodeId}`} className="text-sm font-medium">Add to a Universe</label>
        {choices.length > 1 && (
          <select id={`add-to-universe-pick-${episodeId}`} value={picked} onChange={(e) => setChoice(e.target.value)}
            className={`${field} sm:w-auto`} data-testid={`add-to-universe-pick-${episodeId}`}>
            {choices.map((c) => <option key={c.series_id} value={c.series_id}>{c.name}</option>)}
          </select>
        )}
        <button type="button" onClick={add} disabled={busy || !picked} className={btnSecondary}
          data-testid={`add-to-universe-button-${episodeId}`}>
          {busy ? 'Adding…' : choices.length > 1 ? 'Add' : `Add to ${choices[0].name}`}
        </button>
      </div>
      {problem && (
        <p className="mt-2 text-sm text-red-700 dark:text-red-300" role="alert" data-testid={`add-to-universe-problem-${episodeId}`}>
          {problem}
        </p>
      )}
    </div>
  );
}

function SystemDetails() {
  const [health, setHealth] = useState<Health | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    apiFetch<Health>('/api/health').then(setHealth).catch((e) => setProblem(e instanceof Error ? e.message : String(e)));
  }, []);

  const ok = health?.status === 'ok';
  return (
    <details className={`${card} mt-8`} data-testid="system-details">
      <summary className={`cursor-pointer rounded text-sm font-semibold ${focusRing}`}>System</summary>
      <div className="mt-3 space-y-1 text-sm text-gray-700 dark:text-gray-200">
        <p>API: {ok ? 'Connected' : problem ? `Disconnected (${problem})` : health ? 'Disconnected' : 'Checking…'}</p>
        <p>Temporal: {health?.temporal ? 'Connected' : 'Disconnected'}</p>
        <p className="text-xs text-gray-600 dark:text-gray-300">API URL: {API_URL}</p>
      </div>
      <h3 className="mt-4 text-sm font-semibold">Production safety</h3>
      <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-gray-700 dark:text-gray-200">
        <li>Dry-run is the default mode: no charges until an episode is switched to live mode.</li>
        <li>Live mode requires G1.08 credit plan approval before any paid generation.</li>
        <li>Budget stops automatically at 80% of each line&apos;s cap.</li>
        <li>All gates are fail-closed: missing judge keys escalate to manual review.</li>
        <li>Episode data (beatmaps, continuity) is uploaded, never committed to the public repo.</li>
      </ul>
    </details>
  );
}

export default function StudioHomePage() {
  const { me, isAdmin, loading: meLoading } = useMe();
  const [episodes, setEpisodes] = useState<Episode[] | null>(null);
  const [worlds, setWorlds] = useState<UniverseCard[] | null>(null);
  const [worldsIssue, setWorldsIssue] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [chosenScope, setChosenScope] = useState<'mine' | 'everyone' | null>(null);
  const [summaries, setSummaries] = useState<Record<string, Summary | null>>({});
  const [allFinished, setAllFinished] = useState(false);
  const requested = useRef(new Set<string>());

  const loadWorlds = useCallback(() => apiFetch<{ worlds: UniverseCard[] }>('/api/worlds')
    .then((data) => {
      setWorlds(data.worlds ?? []);
      setWorldsIssue(null);
    })
    .catch((e) => {
      setWorlds([]);
      setWorldsIssue(e instanceof ApiError ? e.message : UNREACHABLE);
    }), []);

  const loadEpisodes = useCallback(() => apiFetch<{ episodes: Episode[] }>('/api/episodes')
    .then((data) => setEpisodes(data.episodes ?? []))
    .catch((e) => {
      if (e instanceof ApiError && e.status === 401) {
        window.location.assign('/login?from=/studio');
        return;
      }
      setError(e instanceof ApiError ? e.message : UNREACHABLE);
    }), []);

  useEffect(() => {
    loadEpisodes();
    loadWorlds();
  }, [loadEpisodes, loadWorlds]);

  // admins look after everyone's videos; members start with their own
  const scope = chosenScope ?? (isAdmin ? 'everyone' : 'mine');
  const myUserId = me?.user_id ?? null;
  const shown = useMemo(
    () => (episodes ?? []).filter((ep) => scope === 'everyone' || (myUserId !== null && ep.user_id === myUserId)),
    [episodes, scope, myUserId],
  );

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

  const viewer = useMemo(() => ({ userId: myUserId, isAdmin }), [myUserId, isAdmin]);
  const finishedIds = useMemo(
    () => new Set(Object.entries(summaries).filter(([, s]) => isFinished(s?.state)).map(([id]) => id)),
    [summaries],
  );
  const sections = useMemo(() => homeSections(worlds ?? [], shown, finishedIds, viewer), [worlds, shown, finishedIds, viewer]);
  const choices = useMemo(() => universeChoices(worlds ?? []), [worlds]);
  const refresh = useCallback(() => {
    loadWorlds();
    loadEpisodes();
  }, [loadWorlds, loadEpisodes]);

  const loading = (episodes === null || worlds === null) && !error;
  const ready = !loading && !meLoading && !error;
  const firstRun = ready && chosenScope === null && !worldsIssue && isFirstRun(worlds, episodes, viewer);
  const othersMade = (episodes ?? []).length > 0;
  const finishedShown = allFinished ? sections.finished : sections.finished.slice(0, FINISHED_SHOWN);

  if (firstRun) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-8 sm:px-6 lg:px-8" data-testid="first-run">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Welcome to the studio</h1>
        <p className="mt-1 text-gray-700 dark:text-gray-200">
          Create a Universe once. Every episode keeps it: the same characters, places and look, episode after episode.
        </p>
        <ol className="mt-4 grid gap-2 text-sm text-gray-700 sm:grid-cols-2 dark:text-gray-200" data-testid="first-run-steps">
          <li className={`${card} !p-3`}><strong>1. References.</strong> Photos and notes: the premise, the characters, the places, the tone.</li>
          <li className={`${card} !p-3`}><strong>2. Mood board.</strong> The studio shows the price first, then makes it.</li>
          <li className={`${card} !p-3`}><strong>3. Approve or redo.</strong> Say what to change until it feels right.</li>
          <li className={`${card} !p-3`}><strong>4. Bible.</strong> Characters and places are written for you; lock each look once.</li>
        </ol>
        <div className={`${card} mt-6`}>
          <UniverseCreateForm />
        </div>
        <p className="mt-4 text-sm text-gray-700 dark:text-gray-200">
          Just want to try it?{' '}
          <Link href="/studio/create" data-testid="make-video-link" className={link}>Skip, just make one video</Link>
          {othersMade && (
            <>
              {' · '}
              <button type="button" className={link} onClick={() => setChosenScope('everyone')} data-testid="scope-everyone">
                See the shared videos
              </button>
            </>
          )}
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Universes</h1>
          <p className="mt-1 text-gray-700 dark:text-gray-200">
            Create a Universe once. Every episode keeps it.
          </p>
        </div>
        <div className="flex flex-col items-start gap-2 sm:items-end">
          <div className="flex flex-wrap gap-2">
            <Link href="/studio/universes/new" data-testid="new-universe-link" className={btnSecondary}>
              New Universe
            </Link>
            <Link href="/studio/create" data-testid="make-video-link" className={btnPrimary}>
              New episode
            </Link>
          </div>
          {isAdmin && (
            <Link href="/studio/episodes/new" className={`text-sm ${link}`} data-testid="advanced-upload-link">
              Advanced upload
            </Link>
          )}
        </div>
      </div>

      {error && <p role="alert" className="mt-4 text-sm text-red-700 dark:text-red-300">{error}</p>}
      {(loading || meLoading) && !error && <p className="mt-4 text-sm text-gray-600 dark:text-gray-300">Loading your studio…</p>}

      {ready && (
        <section className="mt-8" data-testid="universes-section" aria-labelledby="universes-title">
          <h2 id="universes-title" className="mb-3 text-lg font-semibold text-gray-900 dark:text-white">Your Universes</h2>
          {worldsIssue && (
            <p role="alert" className="text-sm text-red-700 dark:text-red-300" data-testid="universes-problem">
              The Universes could not be loaded: {worldsIssue}
            </p>
          )}
          {!worldsIssue && (worlds ?? []).length === 0 && (
            <div className={`${card} text-sm text-gray-700 dark:text-gray-200`} data-testid="universes-empty">
              <p>
                No Universe yet. A Universe keeps your characters, places and look the same in every episode.{' '}
                <Link href="/studio/universes/new" className={link}>Create a Universe</Link>
              </p>
            </div>
          )}
          {(worlds ?? []).length > 0 && (
            <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3" data-testid="universe-list">
              {(worlds ?? []).map((w) => <UniverseTile key={w.series_id} world={w} myUserId={myUserId} />)}
            </ul>
          )}
        </section>
      )}

      {ready && (
        <div className="mt-10 flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
              {scope === 'mine' ? 'Your episodes' : isAdmin ? 'Everyone’s episodes' : 'All episodes you can open'}
            </h2>
            {!isAdmin && (
              <p className="mt-1 text-sm text-gray-600 dark:text-gray-300" data-testid="episodes-scope-note">
                You see the episodes you made, the ones in your Universes and the shared ones.
              </p>
            )}
          </div>
          <div role="group" aria-label="Show videos" className="inline-flex rounded-lg border border-gray-300 bg-white p-0.5 dark:border-gray-600 dark:bg-gray-800">
            {(['mine', 'everyone'] as const).map((value) => (
              <button key={value} type="button" aria-pressed={scope === value} onClick={() => setChosenScope(value)}
                data-testid={`scope-${value}`}
                className={`rounded-md px-3 py-1.5 text-sm font-medium ${focusRing} ${
                  scope === value ? 'bg-blue-600 text-white' : 'text-gray-700 hover:bg-gray-50 dark:text-gray-200 dark:hover:bg-gray-700'
                }`}>
                {value === 'mine' ? 'Mine' : isAdmin ? 'Everyone' : 'All'}
              </button>
            ))}
          </div>
        </div>
      )}

      {ready && shown.length === 0 && (
        <div className={`${card} mt-4 text-center`} data-testid="episodes-empty">
          <p className="text-lg font-semibold text-gray-900 dark:text-white">
            {scope === 'mine' ? 'You have not made a video yet' : 'No videos yet'}
          </p>
          <p className="mt-1 text-gray-700 dark:text-gray-200">
            Open a Universe and start a new episode in it, or pick a style and describe your idea in a few sentences.
          </p>
          {scope === 'mine' && othersMade && (
            <p className="mt-3 text-sm text-gray-600 dark:text-gray-300">
              Other people&apos;s videos you can open are under{' '}
              <button type="button" className={link} onClick={() => setChosenScope('everyone')}>{isAdmin ? 'Everyone' : 'All'}</button>.
            </p>
          )}
        </div>
      )}

      {shown.length > 0 && (
        <div data-testid="episode-list">
          {sections.continue.length > 0 && (
            <section className="mt-6" data-testid="continue-section">
              <SectionHeading title="Continue" text="Episodes on their way: the next step is on each one's page." />
              <ul className="space-y-3">
                {sections.continue.map(({ episode, universe }) => (
                  <EpisodeCard key={episode.episode_id} episode={episode} summary={summaries[episode.episode_id]}
                    myUserId={myUserId} isAdmin={isAdmin} universe={universe} />
                ))}
              </ul>
            </section>
          )}
          {sections.finished.length > 0 && (
            <section className="mt-8" data-testid="finished-section">
              <SectionHeading title="Recently finished" text="Play them here or download them." />
              <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {finishedShown.map(({ episode, universe }) => (
                  <FinishedCard key={episode.episode_id} episode={episode} summary={summaries[episode.episode_id]}
                    myUserId={myUserId} isAdmin={isAdmin} universe={universe}>
                    {!universe && mayChange(episode, viewer) && (
                      <AddToUniverse episodeId={episode.episode_id} choices={choices} onAdded={refresh} />
                    )}
                  </FinishedCard>
                ))}
              </ul>
              {sections.finished.length > finishedShown.length && (
                <button type="button" className={`${btnSecondary} mt-3`} onClick={() => setAllFinished(true)}>
                  Show {sections.finished.length - finishedShown.length} more
                </button>
              )}
            </section>
          )}
          {sections.unfiled.length > 0 && (
            <section className="mt-8" data-testid="unfiled-section">
              <SectionHeading title="Unfiled"
                text="Episodes in no Universe. Add one to a Universe to keep its characters and places for the next episodes." />
              <ul className="space-y-3">
                {sections.unfiled.map(({ episode }) => (
                  <EpisodeCard key={episode.episode_id} episode={episode} summary={summaries[episode.episode_id]}
                    myUserId={myUserId} isAdmin={isAdmin} universe={null}>
                    <AddToUniverse episodeId={episode.episode_id} choices={choices} onAdded={refresh} />
                  </EpisodeCard>
                ))}
              </ul>
            </section>
          )}
        </div>
      )}

      {isAdmin && <SystemDetails />}
    </div>
  );
}
