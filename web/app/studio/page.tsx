'use client';

import Link from 'next/link';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useMe } from '@/components/StudioShell';
import { UniverseCreateForm } from '@/components/UniverseCreateForm';
import { EpisodeSections, ScopeToggle, UNREACHABLE, useSummaries, type Episode } from '@/components/EpisodeLists';
import { EmptyState, GlassPanel, HeroBand, UniverseCard } from '@/components/playful';
import { ApiError, API_URL, apiFetch } from '@/lib/studio-api';
import { homeSections, isFinished, isFirstRun, mayChange, universeChoices } from '@/lib/world.mjs';
import type { UniverseCard as UniverseCardData } from '@/lib/world.mjs';
import { card, link } from '@/lib/ui';

interface Health {
  status?: string;
  temporal?: boolean;
}

/** Recently finished shows this many players at first. */
const FINISHED_SHOWN = 6;

function SystemDetails() {
  const [health, setHealth] = useState<Health | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    apiFetch<Health>('/api/health').then(setHealth).catch((e) => setProblem(e instanceof Error ? e.message : String(e)));
  }, []);

  const ok = health?.status === 'ok';
  return (
    <details className={`${card} mt-10`} data-testid="system-details">
      <summary className="cursor-pointer rounded text-[14px] font-extrabold">System</summary>
      <div className="mt-3 space-y-1 text-[13.5px]">
        <p>API: {ok ? 'Connected' : problem ? `Disconnected (${problem})` : health ? 'Disconnected' : 'Checking…'}</p>
        <p>Temporal: {health?.temporal ? 'Connected' : 'Disconnected'}</p>
        <p className="text-[12px] text-ink-soft">API URL: {API_URL}</p>
      </div>
      <h3 className="mt-4 text-[15px]">Production safety</h3>
      <ul className="mt-1 list-disc space-y-1 pl-5 text-[13.5px]">
        <li>Dry-run is the default mode: no charges until an episode is switched to live mode.</li>
        <li>Live mode requires G1.08 credit plan approval before any paid generation.</li>
        <li>Budget stops automatically at 80% of each line&apos;s cap.</li>
        <li>All gates are fail-closed: missing judge keys escalate to manual review.</li>
        <li>Episode data (beatmaps, continuity) is uploaded, never committed to the public repo.</li>
      </ul>
    </details>
  );
}

/**
 * The studio home, Universes first (DESIGN-TARGETS-v2 §6.1): a sky-and-floor hero with the start
 * panel (the Spark step), the reader's Universes with their progress and next step, then the
 * episodes: Keep going (a decision waiting for you first), Recently finished and the ones in no
 * Universe. A first run (nothing of the reader's own yet) is the hero alone, with a way to just make
 * one video.
 */
export default function StudioHomePage() {
  const { me, isAdmin, loading: meLoading } = useMe();
  const [episodes, setEpisodes] = useState<Episode[] | null>(null);
  const [worlds, setWorlds] = useState<UniverseCardData[] | null>(null);
  const [worldsIssue, setWorldsIssue] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [chosenScope, setChosenScope] = useState<'mine' | 'everyone' | null>(null);

  const loadWorlds = useCallback(() => apiFetch<{ worlds: UniverseCardData[] }>('/api/worlds')
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
  const summaries = useSummaries(shown);

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

  const hero = (
    <section className="relative mt-3 sm:mt-6" aria-labelledby="home-title">
      <HeroBand className="grid gap-6 px-5 pt-6 sm:px-10 sm:pt-10 lg:min-h-[440px] lg:grid-cols-[1fr_370px] lg:pl-[52px]">
        <div className="relative z-[2] lg:pb-16">
          <span className="pf-chip bg-white/75 text-blue-ink">Your stories, kept the same</span>
          <h1 id="home-title" className="mt-3 max-w-[500px] text-[34px] sm:text-[46px]">
            Make a Universe once. Every episode keeps it.
          </h1>
          <p className="mt-2.5 max-w-[470px] text-[15px] text-ink-soft sm:mb-5 sm:mt-3.5 sm:text-[17px]">
            Bring your characters, places and props. We lock their looks and voices, so they stay the same in every
            episode you make.
          </p>
          {!firstRun && (
            <div className="mt-4 flex flex-wrap gap-3">
              <Link href="/studio/create" data-testid="make-video-link" className="pf-btn pf-btn-ghost">
                New episode
              </Link>
              {isAdmin && (
                <Link href="/studio/episodes/new" className="pf-btn pf-btn-quiet" data-testid="advanced-upload-link">
                  Advanced upload
                </Link>
              )}
            </div>
          )}
          <div aria-hidden="true" className="mt-6 flex flex-wrap items-center gap-1.5 sm:gap-2">
            {([['Characters', 'from-blue-soft to-blue'], ['Places', 'from-mustard-soft to-mustard'], ['Props', 'from-green-soft to-green']] as const).map(([word, fill]) => (
              <span key={word} className={`rounded-full border-4 border-sky bg-gradient-to-br px-3 py-1.5 font-display text-[13px] font-bold text-ink sm:px-4 sm:py-2 sm:text-[14px] ${fill}`}>
                {word}
              </span>
            ))}
            <span className="ml-1 hidden size-4 rounded-full bg-coral sm:block" />
          </div>
        </div>
        <GlassPanel className="relative z-[3] -mx-2 mb-5 self-start p-5 sm:mx-0 sm:mb-7 sm:p-[22px]">
          <UniverseCreateForm heading="h2" />
          {firstRun ? (
            <p className="mt-4 text-center text-[13px] text-ink-soft">
              Just want to try it?{' '}
              <Link href="/studio/create" data-testid="make-video-link" className={link}>Skip, just make one video</Link>
              {othersMade && (
                <>
                  {' · '}
                  <button type="button" className={link} onClick={() => setChosenScope('everyone')} data-testid="scope-everyone">
                    See everyone&apos;s videos
                  </button>
                </>
              )}
            </p>
          ) : sections.unfiled.length > 0 && (
            <>
              <div className="my-3.5 flex items-center gap-2.5 text-[12px] font-bold text-ink-soft before:h-[1.5px] before:flex-1 before:bg-ink/10 after:h-[1.5px] after:flex-1 after:bg-ink/10">
                or
              </div>
              <a href="#not-in-a-universe" className="pf-btn pf-btn-peach w-full">Add an older episode to a Universe</a>
            </>
          )}
        </GlassPanel>
      </HeroBand>
    </section>
  );

  if (firstRun) {
    return (
      <div className="mx-auto max-w-[1240px] px-4 pb-12 sm:px-8" data-testid="first-run">
        {hero}
        <ol className="mt-8 grid gap-3 sm:grid-cols-2 lg:grid-cols-4" data-testid="first-run-steps">
          <li className={`${card} !p-4`}><strong>1. References.</strong> Photos and notes: the premise, the characters, the places, the tone.</li>
          <li className={`${card} !p-4`}><strong>2. Mood board.</strong> The studio shows the price first, then makes it.</li>
          <li className={`${card} !p-4`}><strong>3. Approve or redo.</strong> Say what to change until it feels right.</li>
          <li className={`${card} !p-4`}><strong>4. Bible.</strong> Characters and places are written for you; lock each look once.</li>
        </ol>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-[1240px] px-4 pb-12 sm:px-8">
      {hero}

      {error && <p role="alert" className="mt-4 text-[13.5px] text-coral-ink">{error}</p>}
      {(loading || meLoading) && !error && <p className="mt-6 text-[13.5px] text-ink-soft">Loading your studio…</p>}

      {ready && (
        <section className="mt-10" data-testid="universes-section" aria-labelledby="universes-title">
          <div className="mb-4">
            <h2 id="universes-title" className="text-[26px]">Your Universes</h2>
            <p className="text-[13px] text-ink-soft">Only you and people editing their episodes can see these.</p>
          </div>
          {worldsIssue && (
            <p role="alert" className="text-[13.5px] text-coral-ink" data-testid="universes-problem">
              The Universes could not be loaded: {worldsIssue}
            </p>
          )}
          {!worldsIssue && (worlds ?? []).length === 0 && (
            <EmptyState testId="universes-empty" title="No Universe yet"
              action={<Link href="/studio/universes/new" className="pf-btn pf-btn-soft pf-btn-sm" data-testid="new-universe-link">Create a Universe</Link>}>
              A Universe keeps your characters, places and look the same in every episode.
            </EmptyState>
          )}
          {(worlds ?? []).length > 0 && (
            <ul className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3" data-testid="universe-list">
              {(worlds ?? []).map((w) => <UniverseCard key={w.series_id} world={w} myUserId={myUserId} />)}
              <li className="min-w-0">
                <EmptyState className="h-full min-h-[150px]" title="Room for another Universe"
                  action={<Link href="/studio/universes/new" className="pf-btn pf-btn-soft pf-btn-sm" data-testid="new-universe-link">New Universe</Link>}>
                  Start with a name and a spark of an idea.
                </EmptyState>
              </li>
            </ul>
          )}
        </section>
      )}

      {ready && (
        <div className="mt-12 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-[26px]">{scope === 'mine' ? 'Your episodes' : 'Everyone’s episodes'}</h2>
            <Link href="/studio/episodes" className="text-[13px] font-extrabold text-blue-deep">All episodes</Link>
          </div>
          <ScopeToggle scope={scope} onChange={setChosenScope} />
        </div>
      )}

      {ready && shown.length === 0 && (
        <EmptyState className="mt-4" testId="episodes-empty"
          title={scope === 'mine' ? 'You have not made a video yet' : 'No videos yet'}>
          <p>Open a Universe and start a new episode in it, or pick a style and describe your idea in a few sentences.</p>
          {scope === 'mine' && othersMade && (
            <p className="mt-2">
              Other people&apos;s videos are under{' '}
              <button type="button" className={link} onClick={() => setChosenScope('everyone')}>Everyone</button>.
            </p>
          )}
        </EmptyState>
      )}

      {shown.length > 0 && (
        <EpisodeSections sections={sections} summaries={summaries} myUserId={myUserId} isAdmin={isAdmin} choices={choices}
          mayChange={(episode) => mayChange(episode, viewer)} onAdded={refresh} finishedLimit={FINISHED_SHOWN} />
      )}

      {isAdmin && <SystemDetails />}
    </div>
  );
}
