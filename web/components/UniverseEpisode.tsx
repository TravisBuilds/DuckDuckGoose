'use client';

import Link from 'next/link';
import { useState, type ReactNode } from 'react';
import { ConsistencyBadge } from '@/components/ConsistencyBadge';
import Checkout from '@/components/Checkout';
import { Avatar, GateChecklist } from '@/components/playful';
import { FREE_NOTE, calmText, videoQuote, type OfflineEstimate, type VideoPrice } from '@/lib/checkout.mjs';
import { castColours } from '@/lib/castColour.mjs';
import type { CastColour } from '@/lib/castColour.mjs';
import { itemBadge, stripBadge, type Badge } from '@/lib/consistency.mjs';
import { API_URL, ApiError, apiFetch, postJson } from '@/lib/studio-api';
import { btnPrimary, btnSecondary, card, input, link, tones } from '@/lib/ui';
import {
  pendingStart, pendingStartKey, startRequest, universeChoices, type EpisodeUniverse, type StartRequest, type StripItem,
  type UniverseCard,
} from '@/lib/world.mjs';

function problemText(e: unknown): string {
  return e instanceof ApiError ? calmText(e.message) : 'The studio could not be reached. Try again in a moment.';
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
 * "<Universe> / <episode>", or "Universes / <episode> · Not in a Universe · Add to a Universe" for a reader
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
      <span aria-hidden="true">/</span>
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
  return (
    <li className={`flex min-w-0 max-w-full items-center gap-2.5 rounded-full bg-white py-1.5 pl-1.5 pr-3.5 ${ring ? 'shadow-[0_0_0_3px_var(--coral),var(--shadow-1)]' : 'shadow-card'}`}
      data-testid={testId}>
      {image ? (
        <img src={`${API_URL}${image.thumb_url}`} alt="" className="h-10 w-10 flex-none rounded-full object-cover" />
      ) : (
        <Avatar name={name} hue={colour?.colour ?? 'ink'} size={40} />
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
 * unlocked character ringed in orange-red (for a reader who may see its cast). `children` sit at the foot
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
    <div className="mt-4 flex flex-col gap-4 rounded-[22px] border-2 border-coral/40 bg-coral-soft p-4 sm:flex-row sm:items-center"
      data-testid="lock-gate">
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

/** A start an earlier wizard kept in this browser (before the studio kept them), or null. */
function startKeptHere(episodeId: string): StartRequest | null {
  if (typeof window === 'undefined') return null;
  try {
    return pendingStart(window.localStorage.getItem(pendingStartKey(episodeId)), episodeId);
  } catch {
    return null;
  }
}

/** Whether an earlier wizard left a start for this episode in this browser. */
export function hasStartKeptHere(episodeId: string): boolean {
  return startKeptHere(episodeId) !== null;
}

/** "2 of 5 shots start from a frame you picked", or how to pick them, under the start button. */
function framesLine(frames: { picked: number; shots: number }): string {
  if (frames.picked === 0) return 'The studio makes each shot\u2019s first frame while filming, or you can pick them first.';
  if (frames.picked === frames.shots) return 'Every shot starts from a frame you picked.';
  return `${frames.picked} of ${frames.shots} shots start from a frame you picked. The studio makes the others while filming.`;
}

/** A test run's start, when the episode's price has not been read: it is free whatever it holds. */
const FREE_START: VideoPrice = { free: true, words: 'free test run', approve: 0, rows: [] };

/**
 * Starts an episode the wizard left on its page before it started: in a Universe until every character
 * it uses has a locked look, or so its owner can pick its first frames. The start waits for the locks;
 * it sends the request the wizard kept in the studio (on any device), else one an earlier wizard kept
 * in this browser, else a free test run. It shows the video's one price (`price`: the offline estimate
 * of its shots from preflight) next to one button. Starting pays for nothing: a live video is paid for
 * when its shot plan is approved, where the same price is shown again (docs/EASY-CHECKOUT.md).
 */
export function StartCard({ episodeId, info, frames = null, price = null, onStarted }: {
  episodeId: string; info: EpisodeUniverse; frames?: { picked: number; shots: number } | null;
  price?: OfflineEstimate | null; onStarted: () => Promise<void> | void;
}) {
  const [keptHere] = useState<StartRequest | null>(() => startKeptHere(episodeId));
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const inUniverse = Boolean(info.universe);
  const unlocked = inUniverse ? info.lock?.unlocked ?? [] : [];
  const request: StartRequest = startRequest(info.kept_start, episodeId) ?? keptHere
    ?? { episode_id: episodeId, dry_run: true, enable_story_shaping: true };
  const video = videoQuote(price, { free: request.dry_run }) ?? (request.dry_run ? FREE_START : null);

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
  const waitFor = `Turns on once ${lockNames(unlocked)} ${unlocked.length === 1 ? 'is' : 'are'} locked.`;
  return (
    <section data-testid="start-card" aria-labelledby="start-card-title">
      <div className="min-w-0">
        <span className={`pf-chip ${locked ? tones.todo : tones.bad}`}>{locked ? 'Your turn' : 'Waiting for a lock'}</span>
        <h2 id="start-card-title" className="mt-1 text-[20px] leading-tight">Start this video</h2>
      </div>
      {locked && (
        <p className="mt-2 text-[14px]" data-testid="start-ready">
          {inUniverse ? 'Every character in it has a locked look, so it can start.' : 'It waits here until you start it.'}
        </p>
      )}
      {!locked && (
        <GateChecklist className="mt-3" testId="start-checklist" items={[
          { key: 'locked', label: 'Every character locked', done: false,
            detail: `${lockNames(unlocked)} ${unlocked.length === 1 ? 'isn\u2019t' : 'aren\u2019t'} yet` },
        ]} />
      )}
      {frames && frames.shots > 0 && (
        <p className="mt-3 text-[13px]" data-testid="start-frames">
          {framesLine(frames)}{' '}
          {frames.picked < frames.shots && <a href="#first-frames" className={link}>Go to First frames</a>}
        </p>
      )}
      <Checkout className="mt-3" testId="start-checkout" what="This video" price={video} action="Start the video"
        onAction={start} busy={busy} busyAction="Starting…" needsPrice={false} off={locked ? null : waitFor} actionTestId="start-episode"
        lineTestId="start-mode"
        line={request.dry_run ? FREE_NOTE : 'Starting is free. The video is paid for when you approve its shot plan.'}
        rows={video?.rows ?? []}>
        {!request.dry_run && <p>The list price for one try of each picture and clip. A redo is priced on its own.</p>}
      </Checkout>
      {problem && (
        <p role="alert" className="mt-2 text-[13px] font-bold text-coral-ink" data-testid="start-problem">{problem}</p>
      )}
    </section>
  );
}
