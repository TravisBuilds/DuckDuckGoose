'use client';

import Link from 'next/link';
import { useState, type ReactNode } from 'react';
import { ConsistencyBadge } from '@/components/ConsistencyBadge';
import { GateChecklist, GooseArt, PriceBlock, Seal, hueOf } from '@/components/playful';
import { castColours } from '@/lib/castColour.mjs';
import type { CastColour } from '@/lib/castColour.mjs';
import { itemBadge, stripBadge, type Badge } from '@/lib/consistency.mjs';
import { API_URL, ApiError, apiFetch, postJson } from '@/lib/studio-api';
import { btnGo, btnPrimary, btnSecondary, card, input, link, tones } from '@/lib/ui';
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
        <p role="alert" className="font-bold text-coral-ink" data-testid="breadcrumb-add-problem">{problem}</p>
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
    <nav aria-label="Breadcrumb" className="flex flex-wrap items-baseline gap-x-1 text-[13px] font-extrabold text-ink-soft"
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

function StripEntry({ name, image, badge, testId, colour, ring = false }: {
  name: string; image: { thumb_url: string } | null; badge: Badge; testId: string; colour?: CastColour | null; ring?: boolean;
}) {
  const hue = colour ? hueOf(colour) : null;
  return (
    <li className={`flex min-w-0 max-w-full items-center gap-2.5 rounded-full bg-white py-1.5 pl-1.5 pr-3.5 ${ring ? 'shadow-[0_0_0_3px_var(--coral),var(--shadow-1)]' : 'shadow-card'}`}
      data-testid={testId}>
      {image ? (
        <img src={`${API_URL}${image.thumb_url}`} alt="" className="h-10 w-10 flex-none rounded-full object-cover" />
      ) : (
        <span aria-hidden="true" className={`grid h-10 w-10 flex-none place-items-center rounded-full font-display text-[16px] ${hue ? `${hue.soft} ${hue.ink}` : 'bg-[#F2EEE5] text-ink-soft'}`}>
          {name.slice(0, 1)}
        </span>
      )}
      <span className="min-w-0">
        <span className="block truncate text-[13.5px] font-extrabold" title={name}>{name}</span>
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

/**
 * "From <Universe>": what the episode uses from its Universe, each with its lock or photo badge and an
 * unlocked character ringed in coral (for a reader who may see its cast). `children` sit at the foot
 * (the lock gate).
 */
export function UniverseStrip({ info, children }: { info: EpisodeUniverse | null; children?: ReactNode }) {
  if (!info?.universe || !info.strip) return null;
  const { characters, places, props } = info.strip;
  const empty = characters.length + places.length + props.length === 0;
  const colours = castColours(characters);
  return (
    <section className={card} data-testid="universe-strip" aria-labelledby="universe-strip-title">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="universe-strip-title" className="text-[22px]">From {info.universe.name}</h2>
        <p className="text-[12.5px] text-ink-soft">
          What this episode&apos;s shots use from{' '}
          <Link href={`/studio/universes/${encodeURIComponent(info.universe.series_id)}`} className={link}>{info.universe.name}</Link>.
        </p>
      </div>
      {empty ? (
        <p className="mt-3 text-[13.5px] text-ink-soft" data-testid="universe-strip-empty">
          No shot uses a character, place or prop from the Universe yet.
        </p>
      ) : (
        <ul className="mt-3 flex flex-wrap gap-2.5">
          {characters.map((c) => (
            <StripEntry key={`character-${c.slug}-${c.version}`} name={c.version !== 1 ? `${c.name} (look ${c.version})` : c.name}
              image={c.image} badge={stripBadge(c)} testId={`strip-character-${c.slug}`} colour={colours[c.slug]}
              ring={!c.locked} />
          ))}
          {itemEntries(places, 'place')}
          {itemEntries(props, 'prop')}
        </ul>
      )}
      {children}
    </section>
  );
}

/**
 * The lock gate (DESIGN-TARGETS-v2 §6.5): in a Universe every character keeps one locked look, so the
 * video cannot start while one it uses is not locked. The studio enforces it; this card says it in one
 * sentence and points to the cast, where the look is locked with a character sheet.
 */
export function LockGateCard({ info }: { info: EpisodeUniverse }) {
  const unlocked = info.lock?.unlocked ?? [];
  if (unlocked.length === 0) return null;
  const first = unlocked[0].name.split(' ')[0];
  return (
    <div className="mt-4 flex flex-col gap-4 rounded-[22px] border-2 border-[#F4B3AB] bg-coral-soft p-4 sm:flex-row sm:items-center"
      data-testid="lock-gate">
      <Seal state="open" top="not" bottom="locked" size={64} className="shrink-0" />
      <div className="min-w-0 flex-1">
        <h3 className="text-[18px]">
          {unlocked.length === 1 ? `Lock ${first}’s look before this video starts` : 'Lock these looks before this video starts'}
        </h3>
        <p className="mt-1 text-[13.5px]" data-testid="start-lock">
          Lock the look of {lockNames(unlocked)} before this video starts: it is in {info.universe?.name ?? 'a Universe'},
          where every character keeps one locked look in every episode.{' '}
          <a href="#cast" className={link} data-testid="start-lock-link">Go to Cast</a>, make the character sheet and lock it.
        </p>
      </div>
      <a href="#cast" className={`${btnPrimary} shrink-0`} data-testid="lock-gate-link">
        {unlocked.length === 1 ? `Lock ${first}’s look` : 'Lock the looks'}
      </a>
    </div>
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

  const locked = unlocked.length === 0;
  return (
    <section data-testid="start-card" aria-labelledby="start-card-title">
      <div className="flex items-center gap-3">
        <GooseArt className="h-[64px] w-[58px] shrink-0" />
        <div className="min-w-0">
          <span className={`pf-chip ${locked ? tones.todo : tones.bad}`}>{locked ? 'Your OK needed' : 'Waiting for a lock'}</span>
          <h2 id="start-card-title" className="mt-1 text-[20px] leading-tight">Start this video</h2>
        </div>
      </div>
      {locked && (
        <p className="mt-2 text-[14px]" data-testid="start-ready">
          Every character in it has a locked look, so it can start.
        </p>
      )}
      <PriceBlock className="mt-3" total={request.dry_run ? 'Free' : 'Live run'} status={request.dry_run ? 'Test run' : 'Priced when you made it'}
        rows={request.dry_run
          ? [{ label: 'Pictures and clips', value: 'Free stand-ins' }]
          : [{ label: 'Pictures, clips and voices', value: 'The price you confirmed' }]}>
        <p className="mt-2 text-[12.5px]" data-testid="start-mode">
          {request.dry_run
            ? 'It starts as a free test run: no credits are used.'
            : 'It starts as the live run you confirmed when you made it: it uses credits.'}
        </p>
      </PriceBlock>
      <GateChecklist className="mt-3" testId="start-checklist" items={[
        { key: 'price', label: request.dry_run ? 'Free test run' : 'Price confirmed when you made it', done: true },
        { key: 'locked', label: 'Every character locked', done: locked,
          detail: locked ? null : `${lockNames(unlocked)} ${unlocked.length === 1 ? 'isn\u2019t' : 'aren\u2019t'} yet` },
      ]} />
      <button type="button" className={`${btnGo} mt-3 w-full`} disabled={busy || !locked} onClick={start}
        data-testid="start-episode">
        {busy ? 'Starting…' : 'Start the video'}
      </button>
      <p className="mt-2 text-center text-[12px] text-ink-soft">
        {locked
          ? request.dry_run ? 'Nothing is charged in a test run.' : 'The studio checks the price and your balance again before each paid step.'
          : `Turns on once ${lockNames(unlocked)} ${unlocked.length === 1 ? 'is' : 'are'} locked.`}
      </p>
      {problem && (
        <p role="alert" className="mt-2 text-[13px] font-bold text-coral-ink" data-testid="start-problem">{problem}</p>
      )}
    </section>
  );
}
