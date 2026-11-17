'use client';

import Link from 'next/link';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useMe } from '@/components/StudioShell';
import { EpisodeSections, ScopeToggle, UNREACHABLE, useSummaries, type Episode } from '@/components/EpisodeLists';
import { EmptyState, Icon } from '@/components/playful';
import { ApiError, apiFetch } from '@/lib/studio-api';
import { homeSections, isFinished, mayChange, universeChoices } from '@/lib/world.mjs';
import type { UniverseCard } from '@/lib/world.mjs';

/** Every episode the reader may see, in the home's three lists, all finished ones shown. */
export default function EpisodesPage() {
  const { me, isAdmin, loading: meLoading } = useMe();
  const [episodes, setEpisodes] = useState<Episode[] | null>(null);
  const [worlds, setWorlds] = useState<UniverseCard[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [chosenScope, setChosenScope] = useState<'mine' | 'everyone' | null>(null);

  const load = useCallback(() => {
    apiFetch<{ episodes: Episode[] }>('/api/episodes')
      .then((data) => setEpisodes(data.episodes ?? []))
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) {
          window.location.assign('/login?from=/studio/episodes');
          return;
        }
        setError(e instanceof ApiError ? e.message : UNREACHABLE);
      });
    apiFetch<{ worlds: UniverseCard[] }>('/api/worlds').then((data) => setWorlds(data.worlds ?? [])).catch(() => setWorlds([]));
  }, []);
  useEffect(() => {
    load();
  }, [load]);

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
  const sections = useMemo(() => homeSections(worlds, shown, finishedIds, viewer), [worlds, shown, finishedIds, viewer]);
  const ready = episodes !== null && !meLoading && !error;

  return (
    <div className="mx-auto max-w-[1240px] px-4 pb-12 pt-8 sm:px-8" data-testid="episodes-page">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-[34px] sm:text-[40px]">Episodes</h1>
          <p className="text-[14px] text-ink-soft">Everything on its way and everything finished, the ones waiting for you first.</p>
          {!isAdmin && (
            <p className="text-[13px] text-ink-soft" data-testid="episodes-scope-note">
              You see the episodes you made, the ones in your Universes and the shared ones.
            </p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <ScopeToggle scope={scope} onChange={setChosenScope} isAdmin={isAdmin} />
          <Link href="/studio/create" className="pf-btn pf-btn-primary pf-btn-sm"><Icon name="plus" size={14} />New episode</Link>
        </div>
      </div>
      {error && <p role="alert" className="mt-4 text-[13.5px] text-coral-ink">{error}</p>}
      {!ready && !error && <p className="mt-6 text-[13.5px] text-ink-soft">Loading the episodes…</p>}
      {ready && shown.length === 0 && (
        <EmptyState className="mt-6" testId="episodes-empty" title={scope === 'mine' ? 'You have not made a video yet' : 'No videos yet'}
          action={<Link href="/studio/create" className="pf-btn pf-btn-soft pf-btn-sm"><Icon name="plus" size={14} />Make one</Link>}>
          Open a Universe and start a new episode in it, or pick a style and describe your idea.
        </EmptyState>
      )}
      {shown.length > 0 && (
        <EpisodeSections sections={sections} summaries={summaries} myUserId={myUserId} isAdmin={isAdmin}
          choices={universeChoices(worlds)} mayChange={(episode) => mayChange(episode, viewer)} onAdded={load} finishedLimit={null} />
      )}
    </div>
  );
}
