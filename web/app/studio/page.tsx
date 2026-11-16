'use client';

import Link from 'next/link';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useMe } from '@/components/StudioShell';
import { getPresetById } from '@/lib/presets';
import { ApiError, API_URL, apiFetch } from '@/lib/studio-api';
import { formatLocalDate } from '@/lib/time';
import { ownerLabel } from '@/lib/labels';
import { nextStep, statusChip } from '@/lib/next-step.mjs';
import type { RunStateLike, StoryLike } from '@/lib/next-step.mjs';
import { btnPrimary, card, chip, focusRing, link, tones, type Tone } from '@/lib/ui';

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

type EpisodeState = RunStateLike & { archetype_id?: string | null };
type StoryShape = StoryLike & { story_shape?: { logline?: string } | null };

/** Each status chip costs a run-state read, so only the first cards get one. */
const STATUS_LIMIT = 20;
const PARALLEL_READS = 4;

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

/** The card's status chip. A step only its owner (or an admin) can take says who it waits on. */
function chipFor(summary: Summary, episode: Episode, myUserId: string | null, isAdmin: boolean): { label: string; tone: Tone } {
  if (!summary.state) return { label: 'Not started', tone: 'wait' };
  const mine = episode.user_id === null || episode.user_id === myUserId;
  const viewer = { mine, canAct: isAdmin || mine, ownerName: episode.owner_name };
  return statusChip(nextStep(summary.state, summary.story, null, { viewer }));
}

function EpisodeCard({ episode, summary, myUserId, isAdmin }: {
  episode: Episode; summary: Summary | null | undefined; myUserId: string | null; isAdmin: boolean;
}) {
  const date = formatLocalDate(episode.created_at);
  const owner = ownerLabel(episode.user_id, episode.owner_name, myUserId);
  const status = summary ? chipFor(summary, episode, myUserId, isAdmin) : null;
  return (
    <li>
      <Link href={`/studio/episodes/${episode.episode_id}`} data-testid={`episode-card-${episode.episode_id}`}
        className={`block rounded-lg border border-gray-200 bg-white p-4 transition-colors hover:border-blue-400 hover:bg-blue-50/40 dark:border-gray-700 dark:bg-gray-800 dark:hover:bg-gray-700 ${focusRing}`}>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="font-semibold text-gray-900 dark:text-white">
            <span className="font-mono">{episode.episode_id}</span>
            {summary?.style && <span className="font-normal text-gray-700 dark:text-gray-200"> · {summary.style}</span>}
          </h3>
          {status && <span className={`${chip} ${tones[status.tone]}`} data-testid={`episode-status-${episode.episode_id}`}>{status.label}</span>}
        </div>
        {summary?.logline && <p className="mt-1 line-clamp-2 text-sm text-gray-700 dark:text-gray-200">{summary.logline}</p>}
        <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-gray-600 dark:text-gray-300">
          <span data-testid={`episode-owner-${episode.episode_id}`}>{owner === 'You' ? 'By you' : `By ${owner}`}</span>
          {date && <span>{date}</span>}
          {episode.live_mode && <span className={`${chip} ${tones.warn}`}>Live mode</span>}
          {isAdmin && episode.g108_approved && <span className={`${chip} ${tones.ok}`}>G1.08 approved</span>}
        </p>
      </Link>
    </li>
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
  const [error, setError] = useState<string | null>(null);
  const [chosenScope, setChosenScope] = useState<'mine' | 'everyone' | null>(null);
  const [summaries, setSummaries] = useState<Record<string, Summary | null>>({});
  const requested = useRef(new Set<string>());

  useEffect(() => {
    apiFetch<{ episodes: Episode[] }>('/api/episodes')
      .then((data) => setEpisodes(data.episodes ?? []))
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) {
          window.location.assign('/login?from=/studio');
          return;
        }
        setError(e instanceof ApiError ? e.message : 'The studio could not be reached. Check your connection and reload.');
      });
  }, []);

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

  const loading = episodes === null && !error;

  return (
    <div className="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">My videos</h1>
          <p className="mt-1 text-gray-700 dark:text-gray-200">
            Describe an idea, approve each step, then download your video.
          </p>
        </div>
        <div className="flex flex-col items-end gap-2">
          <Link href="/studio/create" data-testid="make-video-link" className={btnPrimary}>
            Make a new video
          </Link>
          {isAdmin && (
            <Link href="/studio/episodes/new" className={`text-sm ${link}`} data-testid="advanced-upload-link">
              Advanced upload
            </Link>
          )}
        </div>
      </div>

      <div className="mt-8 flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
          {scope === 'mine' ? 'Videos you made' : 'Everyone’s videos'}
        </h2>
        <div role="group" aria-label="Show videos" className="inline-flex rounded-lg border border-gray-300 bg-white p-0.5 dark:border-gray-600 dark:bg-gray-800">
          {(['mine', 'everyone'] as const).map((value) => (
            <button key={value} type="button" aria-pressed={scope === value} onClick={() => setChosenScope(value)}
              data-testid={`scope-${value}`}
              className={`rounded-md px-3 py-1.5 text-sm font-medium ${focusRing} ${
                scope === value ? 'bg-blue-600 text-white' : 'text-gray-700 hover:bg-gray-50 dark:text-gray-200 dark:hover:bg-gray-700'
              }`}>
              {value === 'mine' ? 'Mine' : 'Everyone'}
            </button>
          ))}
        </div>
      </div>

      {error && <p role="alert" className="mt-4 text-sm text-red-700 dark:text-red-300">{error}</p>}
      {(loading || meLoading) && !error && <p className="mt-4 text-sm text-gray-600 dark:text-gray-300">Loading your videos…</p>}

      {!loading && !meLoading && !error && shown.length === 0 && (
        <div className={`${card} mt-4 text-center`} data-testid="episodes-empty">
          <p className="text-lg font-semibold text-gray-900 dark:text-white">
            {scope === 'mine' ? 'You have not made a video yet' : 'No videos yet'}
          </p>
          <p className="mt-1 text-gray-700 dark:text-gray-200">
            Pick a style, describe your idea in a few sentences, and the studio drafts the story and the shots for you.
          </p>
          <Link href="/studio/create" className={`${btnPrimary} mt-4`}>Make your first video</Link>
          {scope === 'mine' && (episodes ?? []).length > 0 && (
            <p className="mt-3 text-sm text-gray-600 dark:text-gray-300">
              Other people&apos;s videos are under{' '}
              <button type="button" className={link} onClick={() => setChosenScope('everyone')}>Everyone</button>.
            </p>
          )}
        </div>
      )}

      {shown.length > 0 && (
        <ul className="mt-4 space-y-3" data-testid="episode-list">
          {shown.map((ep) => (
            <EpisodeCard key={ep.episode_id} episode={ep} summary={summaries[ep.episode_id]} myUserId={myUserId} isAdmin={isAdmin} />
          ))}
        </ul>
      )}

      {isAdmin && <SystemDetails />}
    </div>
  );
}
