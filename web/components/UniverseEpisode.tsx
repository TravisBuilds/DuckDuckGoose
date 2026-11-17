'use client';

import Link from 'next/link';
import { useState } from 'react';
import { ConsistencyBadge } from '@/components/ConsistencyBadge';
import { itemBadge, stripBadge, type Badge } from '@/lib/consistency.mjs';
import { API_URL, ApiError, apiFetch, postJson } from '@/lib/studio-api';
import { btnPrimary, btnSecondary, card, highlight, input, link } from '@/lib/ui';
import {
  pendingStart, pendingStartKey, universeChoices, type EpisodeUniverse, type StartRequest, type StripItem, type UniverseCard,
} from '@/lib/world.mjs';

function problemText(e: unknown): string {
  return e instanceof ApiError ? e.message : 'The studio could not be reached. Try again in a moment.';
}

/** "Mira Vale", "Mira Vale and Oskar (look 2)", "A, B and C". */
function lockNames(unlocked: { name: string; version: number }[]): string {
  const names = unlocked.map((c) => (c.version !== 1 ? `${c.name} (look ${c.version})` : c.name));
  return names.length <= 1 ? names.join('') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

/** Adds an episode that is in no Universe to one of the reader's (POST /api/episodes/{id}/series). */
function AddToUniverse({ episodeId, onLinked }: { episodeId: string; onLinked: () => Promise<void> | void }) {
  const [open, setOpen] = useState(false);
  const [choices, setChoices] = useState<{ series_id: string; name: string }[] | null>(null);
  const [pick, setPick] = useState('');
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const show = async () => {
    setOpen(true);
    setProblem(null);
    try {
      const body = await apiFetch<{ worlds: UniverseCard[] }>('/api/worlds');
      const list = universeChoices(body.worlds);
      setChoices(list);
      setPick(list[0]?.series_id ?? '');
    } catch (e) {
      setChoices([]);
      setProblem(problemText(e));
    }
  };

  const add = async () => {
    setBusy(true);
    setProblem(null);
    try {
      await postJson(`/api/episodes/${episodeId}/series`, { series_id: pick });
      setOpen(false);
      await onLinked();
    } catch (e) {
      setProblem(problemText(e));
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button type="button" className={link} onClick={show} data-testid="breadcrumb-add-universe">Add to a Universe</button>
    );
  }
  return (
    <div className="mt-2 w-full space-y-2" data-testid="breadcrumb-add-form">
      {choices === null ? (
        <p role="status">Loading your Universes…</p>
      ) : choices.length === 0 ? (
        <p>
          You have no Universe to add it to yet.{' '}
          <Link href="/studio/universes/new" className={link} data-testid="breadcrumb-add-create">Create a Universe</Link>
        </p>
      ) : (
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <label className="sr-only" htmlFor="breadcrumb-add-pick">Universe</label>
          <select id="breadcrumb-add-pick" className={`${input} sm:max-w-xs`} value={pick} onChange={(e) => setPick(e.target.value)}
            data-testid="breadcrumb-add-pick">
            {choices.map((c) => <option key={c.series_id} value={c.series_id}>{c.name}</option>)}
          </select>
          <button type="button" className={btnPrimary} disabled={busy || !pick} onClick={add} data-testid="breadcrumb-add-button">
            {busy ? 'Adding…' : 'Add'}
          </button>
          <button type="button" className={btnSecondary} disabled={busy} onClick={() => setOpen(false)}>Cancel</button>
        </div>
      )}
      {problem && (
        <p role="alert" className="text-red-700 dark:text-red-300" data-testid="breadcrumb-add-problem">{problem}</p>
      )}
    </div>
  );
}

/**
 * "<Universe> › <episode>", or "Universes › <episode> · Not in a Universe · Add to a Universe" for a reader
 * who may see the episode's cast; the Universe is not named to anyone else.
 */
export function EpisodeBreadcrumb({ episodeId, info, canAct, onLinked }: {
  episodeId: string; info: EpisodeUniverse | null; canAct: boolean; onLinked: () => Promise<void> | void;
}) {
  const universe = info?.universe ?? null;
  return (
    <nav aria-label="Breadcrumb" className="flex flex-wrap items-baseline gap-x-1 text-sm text-gray-600 dark:text-gray-300"
      data-testid="episode-breadcrumb">
      {universe ? (
        <Link href={`/studio/universes/${encodeURIComponent(universe.series_id)}`} className={`${link} break-words`}
          data-testid="breadcrumb-universe">
          {universe.name}
        </Link>
      ) : (
        <Link href="/studio" className={link}>Universes</Link>
      )}
      <span aria-hidden="true">›</span>
      <span aria-current="page" className="break-all">{episodeId}</span>
      {info?.linked === false && (
        <>
          <span aria-hidden="true">·</span>
          <span data-testid="breadcrumb-unfiled">Not in a Universe</span>
          {canAct && (
            <>
              <span aria-hidden="true">·</span>
              <AddToUniverse episodeId={episodeId} onLinked={onLinked} />
            </>
          )}
        </>
      )}
    </nav>
  );
}

function StripEntry({ name, image, badge, testId }: {
  name: string; image: { thumb_url: string } | null; badge: Badge; testId: string;
}) {
  return (
    <li className="flex w-full min-w-0 items-center gap-3 rounded-lg border border-gray-200 p-2 sm:w-64 dark:border-gray-700"
      data-testid={testId}>
      {image ? (
        <img src={`${API_URL}${image.thumb_url}`} alt="" className="h-12 w-12 flex-none rounded-md object-cover" />
      ) : (
        <span aria-hidden="true" className="h-12 w-12 flex-none rounded-md bg-gray-100 dark:bg-gray-700" />
      )}
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium text-gray-900 dark:text-white" title={name}>{name}</span>
        <ConsistencyBadge badge={badge} testId={`${testId}-badge`} />
      </span>
    </li>
  );
}

function itemEntries(items: StripItem[], kind: 'place' | 'prop') {
  return items.map((item) => (
    <StripEntry key={`${kind}-${item.slug}`} name={item.name} image={item.image} badge={itemBadge(item)}
      testId={`strip-${kind}-${item.slug}`} />
  ));
}

/** What the episode uses from its Universe, with a badge on each (for a reader who may see its cast). */
export function UniverseStrip({ info }: { info: EpisodeUniverse | null }) {
  if (!info?.universe || !info.strip) return null;
  const { characters, places, props } = info.strip;
  const empty = characters.length + places.length + props.length === 0;
  return (
    <section className={card} data-testid="universe-strip" aria-labelledby="universe-strip-title">
      <h2 id="universe-strip-title" className="text-lg font-semibold">From your Universe</h2>
      <p className="mt-1 text-sm text-gray-700 dark:text-gray-200">
        What this episode&apos;s shots use from{' '}
        <Link href={`/studio/universes/${encodeURIComponent(info.universe.series_id)}`} className={link}>{info.universe.name}</Link>.
      </p>
      {empty ? (
        <p className="mt-3 text-sm text-gray-700 dark:text-gray-200" data-testid="universe-strip-empty">
          No shot uses a character, place or prop from the Universe yet.
        </p>
      ) : (
        <ul className="mt-3 flex flex-wrap gap-3">
          {characters.map((c) => (
            <StripEntry key={`character-${c.slug}-${c.version}`} name={c.version !== 1 ? `${c.name} (look ${c.version})` : c.name}
              image={c.image} badge={stripBadge(c)} testId={`strip-character-${c.slug}`} />
          ))}
          {itemEntries(places, 'place')}
          {itemEntries(props, 'prop')}
        </ul>
      )}
    </section>
  );
}

function keptStart(episodeId: string): StartRequest | null {
  if (typeof window === 'undefined') return null;
  try {
    return pendingStart(window.localStorage.getItem(pendingStartKey(episodeId)), episodeId);
  } catch {
    return null;
  }
}

/**
 * Starts an episode in a Universe that has not started yet (the wizard leaves it here while a look is
 * not locked). The start waits until every character it uses has a locked look; it sends the request
 * the wizard kept, else a free test run.
 */
export function StartCard({ episodeId, info, onStarted }: {
  episodeId: string; info: EpisodeUniverse; onStarted: () => Promise<void> | void;
}) {
  const [kept] = useState<StartRequest | null>(() => keptStart(episodeId));
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const unlocked = info.lock?.unlocked ?? [];
  const request: StartRequest = kept ?? { episode_id: episodeId, dry_run: true, enable_story_shaping: true };

  const start = async () => {
    setBusy(true);
    setProblem(null);
    try {
      await postJson('/api/episodes', request);
      try {
        window.localStorage.removeItem(pendingStartKey(episodeId));
      } catch {
        // nothing kept to clear
      }
      await onStarted();
    } catch (e) {
      setProblem(problemText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className={highlight} data-testid="start-card" aria-labelledby="start-card-title">
      <h2 id="start-card-title" className="text-xl font-semibold">Start this video</h2>
      {unlocked.length > 0 ? (
        <p className="mt-1 text-gray-700 dark:text-gray-200" data-testid="start-lock">
          Lock the look of {lockNames(unlocked)} before this video starts: it is in {info.universe?.name ?? 'a Universe'},
          where every character keeps one locked look in every episode.{' '}
          <a href="#cast" className={link} data-testid="start-lock-link">Go to Cast</a>, make the character sheet and lock it.
        </p>
      ) : (
        <p className="mt-1 text-gray-700 dark:text-gray-200" data-testid="start-ready">
          Every character in it has a locked look, so it can start.
        </p>
      )}
      <p className="mt-2 text-sm text-gray-700 dark:text-gray-200" data-testid="start-mode">
        {request.dry_run
          ? 'It starts as a free test run: no credits are used.'
          : 'It starts as the live run you confirmed when you made it: it uses credits.'}
      </p>
      <button type="button" className={`${btnPrimary} mt-4 w-full sm:w-auto`} disabled={busy || unlocked.length > 0} onClick={start}
        data-testid="start-episode">
        {busy ? 'Starting…' : 'Start the video'}
      </button>
      {problem && (
        <p role="alert" className="mt-2 text-sm text-red-700 dark:text-red-300" data-testid="start-problem">{problem}</p>
      )}
    </section>
  );
}
