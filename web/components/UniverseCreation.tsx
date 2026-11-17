'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useRef, useState, type DragEvent, type ReactNode } from 'react';
import { API_URL, ApiError, apiFetch, postJson } from '@/lib/studio-api';
import { formatUsd } from '@/lib/money';
import { formatUsdMicros } from '@/lib/balance.mjs';
import { formatLocalDate } from '@/lib/time';
import { getPresetById, presets } from '@/lib/presets';
import { groupByTemplate } from '@/lib/wizard.mjs';
import { healthLine, driftCount } from '@/lib/consistency.mjs';
import { castColours } from '@/lib/castColour.mjs';
import { PICTURE_ACCEPT, emptyRights, pictureProblem, rightsFields, rightsProblem } from '@/lib/cast.mjs';
import type { RightsAnswers } from '@/lib/cast.mjs';
import { BOARD_POLL_MS, boardEstimateLines, boardProgress } from '@/lib/world.mjs';
import type { BoardEstimate, WorldBoard, WorldBoardRequest, WorldView } from '@/lib/world.mjs';
import {
  CREATION_NAMES, boardUnlockHint, creationJourney, screenSteps, sealFor, stepFromQuery, stepNumber, toneChips,
  type CreationStep,
} from '@/lib/journey.mjs';
import { input as field, label as fieldLabel, tones } from '@/lib/ui';
import { ConfirmButton } from './ConfirmButton';
import { RightsQuestions } from './RightsQuestions';
import { useStudioFocus } from './StudioShell';
import {
  CastDot, EmptyState, GlassPanel, HeroBand, Icon, IconCircle, JourneyPath, PinBoard, PriceBlock, RefTile, Seal,
  StickyNote, TOPIC_STICKY,
} from './playful';

/**
 * Making a Universe, one step per screen (DESIGN-TARGETS-v2 §6.3): Spark, References, Mood board,
 * Bible and Lock at /studio/universes/{id}/create?step=…, for its owner and admins. Every step is an
 * API route (api/world_routes.py), which stays the authority: a refused step shows the API's reason.
 * Only pressing Make spends, for the price shown first; the one-liner and kind of video save as you
 * type (free), and an unsent note waits on this device. A live board is made by the worker picture
 * by picture: the page asks again every BOARD_POLL_MS until it is made.
 */

const UNREACHABLE = 'The studio could not be reached. Check your connection and try again.';
const BOARD_STARTED = 'The studio is making the mood board now. The pictures appear here when they are made.';
const TOPIC_TEXT: Record<string, { label: string; placeholder: string }> = {
  premise: { label: 'Premise', placeholder: 'What the Universe is about, who wants what, what keeps happening' },
  characters: { label: 'Characters', placeholder: 'Who lives here: names, ages, how they look and act' },
  places: { label: 'Places', placeholder: 'Where it happens: rooms, streets, landscapes, the weather' },
  tone: { label: 'Tone', placeholder: 'How it feels: funny, cosy, eerie; the colours and light' },
};
const TITLES: Record<CreationStep, string> = {
  spark: 'Name your Universe',
  references: 'Gather your references',
  moodboard: 'Make your mood board',
  bible: 'Write the bible',
  lock: 'Lock the looks',
};
const CARD_GROUPS = groupByTemplate(presets);
const hint = 'text-[12.5px] text-ink-soft';

type Note = { tone: 'ok' | 'problem'; text: string; where: string };
type BibleResult = { created: Record<string, string[]>; skipped: string[]; dropped: string[] };

function problemText(e: unknown): string {
  return e instanceof ApiError ? e.message : UNREACHABLE;
}

function megabytes(bytes: number): number {
  return Math.round(bytes / 1_048_576);
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function Message({ note, where }: { note: Note | null; where: string }) {
  if (!note || note.where !== where) return null;
  return (
    <p data-testid={`creation-message-${where}`} data-tone={note.tone} role={note.tone === 'problem' ? 'alert' : 'status'}
      className={`mt-3 rounded-inner p-3 text-[13.5px] font-bold ${note.tone === 'ok' ? 'bg-green-soft text-green-ink' : 'bg-coral-soft text-coral-ink'}`}>
      {note.text}
    </p>
  );
}

function BoardPictures({ board, dropping, testId }: { board: WorldBoard; dropping: boolean; testId?: string }) {
  return (
    <PinBoard testId={testId} dropping={dropping}
      pictures={board.images.map((image) => ({
        key: image.image_id,
        src: `${API_URL}${image.thumb_url}`,
        href: `${API_URL}${image.url}`,
        alt: `Mood board ${board.version}, picture ${image.position + 1}`,
        caption: image.placeholder ? 'Stand-in (preview)' : null,
      }))} />
  );
}

function SideNext({ href, children, disabled = false, reason = null, testId = 'creation-next' }: {
  href: string; children: ReactNode; disabled?: boolean; reason?: string | null; testId?: string;
}) {
  return disabled ? (
    <>
      <button type="button" disabled className="pf-btn pf-btn-primary mt-4 w-full" title={reason ?? undefined}
        aria-describedby={reason ? `${testId}-why` : undefined} data-testid={testId}>{children}</button>
      {reason && <p id={`${testId}-why`} className="pf-off-why text-center" data-testid={`${testId}-why`}>{reason}</p>}
    </>
  ) : (
    <Link href={href} className="pf-btn pf-btn-primary mt-4 w-full" data-testid={testId}>{children}</Link>
  );
}

export function UniverseCreation({ seriesId, stepParam }: { seriesId: string; stepParam: string | null }) {
  const router = useRouter();
  const base = `/api/worlds/${encodeURIComponent(seriesId)}`;
  const here = `/studio/universes/${encodeURIComponent(seriesId)}`;
  const stepHref = (key: CreationStep) => `${here}/create?step=${key}`;
  const [view, setView] = useState<WorldView | null>(null);
  const [problem, setProblem] = useState<{ status: number; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<Note | null>(null);
  const [topic, setTopic] = useState('premise');
  const [text, setText] = useState('');
  const [editing, setEditing] = useState<{ id: string; body: string } | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [caption, setCaption] = useState('');
  const [rights, setRights] = useState<RightsAnswers>(emptyRights());
  const [inputKey, setInputKey] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [feedback, setFeedback] = useState('');
  const [estimate, setEstimate] = useState<{ feedback: string; result: BoardEstimate } | null>(null);
  const [bible, setBible] = useState<BibleResult | null>(null);
  const [justMade, setJustMade] = useState<string | null>(null);
  const [spark, setSpark] = useState<{ logline: string; preset: string } | null>(null);
  const [sparkSaving, setSparkSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      setView(await apiFetch<WorldView>(base));
      setProblem(null);
    } catch (e) {
      setProblem(e instanceof ApiError ? { status: e.status, text: e.message } : { status: 0, text: UNREACHABLE });
    }
  }, [base]);
  useEffect(() => {
    load();
  }, [load]);

  const creation = view?.creation ?? null;
  const journey = creationJourney(view);
  const step = stepFromQuery(stepParam, journey);
  const progress = boardProgress(creation?.board_request);
  const making = progress?.kind === 'making';
  useEffect(() => {
    if (!making) return undefined;
    const timer = setInterval(() => {
      load().catch(() => undefined);
    }, BOARD_POLL_MS);
    return () => clearInterval(timer);
  }, [making, load]);

  // the unsent note waits on this device until it is added
  const draftKey = `ddg:universe-note:${seriesId}`;
  const restored = useRef(false);
  useEffect(() => {
    try {
      const kept = JSON.parse(window.localStorage.getItem(draftKey) ?? 'null');
      if (kept && typeof kept.text === 'string') {
        setText(kept.text);
        if (typeof kept.topic === 'string') setTopic(kept.topic);
      }
    } catch {
      // nothing kept
    }
    restored.current = true;
  }, [draftKey]);
  useEffect(() => {
    if (!restored.current) return;
    try {
      if (text.trim()) window.localStorage.setItem(draftKey, JSON.stringify({ topic, text }));
      else window.localStorage.removeItem(draftKey);
    } catch {
      // storage refused: the draft just is not kept
    }
  }, [draftKey, topic, text]);

  useEffect(() => {
    if (view && spark === null) setSpark({ logline: view.world.profile.logline ?? '', preset: view.world.profile.preset_id ?? '' });
  }, [view, spark]);
  const saveProfile = useCallback(async (changes: { logline?: string; preset_id?: string | null }) => {
    setSparkSaving(true);
    try {
      await postJson(`${base}/profile`, changes);
      await load();
    } catch (e) {
      setNote({ tone: 'problem', text: problemText(e), where: 'spark' });
    } finally {
      setSparkSaving(false);
    }
  }, [base, load]);
  const savedLogline = view?.world.profile.logline ?? '';
  const lastSent = useRef<string | null>(null);
  useEffect(() => {
    const wanted = spark?.logline.trim();
    if (wanted === undefined || wanted === savedLogline || wanted === lastSent.current) return undefined;
    const timer = setTimeout(() => {
      lastSent.current = wanted;
      saveProfile({ logline: wanted });
    }, 800);
    return () => clearTimeout(timer);
  }, [spark, savedLogline, saveProfile]);

  useStudioFocus(view ? {
    label: 'New Universe',
    name: view.world.name,
    saved: busy || sparkSaving ? 'saving' : 'saved',
    exitHref: '/studio',
  } : null);

  if (!view) {
    if (problem?.status === 404) {
      return (
        <div className="mx-auto max-w-xl px-4 py-16 text-center" data-testid="universe-not-found">
          <IconCircle icon="search" size={80} className="mx-auto" />
          <h1 className="mt-4 text-[28px]">This Universe could not be found</h1>
          <p className="mt-2 text-ink-soft">It may have been removed, or you may not have access to it. Ask its owner if you think you should.</p>
          <Link href="/studio" className="pf-btn pf-btn-primary mt-6">Back to the studio</Link>
        </div>
      );
    }
    return (
      <div className="mx-auto max-w-[1240px] px-4 py-8 sm:px-8">
        {problem
          ? <p role="alert" className="text-[13.5px] text-coral-ink">{problem.text}</p>
          : <p className="text-[13.5px] text-ink-soft">Loading the Universe…</p>}
      </div>
    );
  }

  const world = view.world;
  if (!view.can_edit || !creation) {
    return (
      <div className="mx-auto max-w-xl px-4 py-16 text-center" data-testid="creation-owner-only">
        <IconCircle icon="lock" size={80} className="mx-auto" />
        <h1 className="mt-4 text-[28px]">Only its owner builds this Universe</h1>
        <p className="mt-2 text-ink-soft">The owner of {world.name}, or an admin, adds its references and mood board.</p>
        <Link href={here} className="pf-btn pf-btn-primary mt-6">Open the Universe</Link>
      </div>
    );
  }

  const act = async <T,>(where: string, fn: () => Promise<T>, done: string | ((result: T) => string)): Promise<T | null> => {
    setBusy(true);
    setNote(null);
    try {
      const result = await fn();
      setNote({ tone: 'ok', text: typeof done === 'string' ? done : done(result), where });
      await load();
      return result;
    } catch (e) {
      setNote({ tone: 'problem', text: problemText(e), where });
      await load().catch(() => undefined);
      return null;
    } finally {
      setBusy(false);
    }
  };

  const { counts, latest, approved, bibleDone } = journey;
  const limits = creation.limits;
  const fileIssue = file ? pictureProblem(file, view.limits.max_upload_bytes) : null;
  const rightsIssue = rightsProblem(rights);
  const waiting = latest?.status === 'ready' ? latest : null;
  const preset = getPresetById(world.profile.preset_id ?? '');
  const drift = driftCount(view.characters);
  const healthTone = drift > 0 ? tones.bad : world.counts.characters === 0 ? tones.wait
    : world.counts.locked_characters >= world.counts.characters ? tones.ok : tones.warn;
  const unlockHint = boardUnlockHint(counts.notes, counts.photos);
  const lockEpisode = view.episodes.find((e) => !e.finished && e.can_change)?.episode_id ?? null;
  const colours = castColours(view.characters);

  const addNote = () => act('notes', () => postJson(`${base}/notes`, { topic, body: text.trim() }), 'Note added.')
    .then((made) => { if (made) setText(''); });
  const saveNote = () => editing && act('notes', () => postJson(`${base}/notes/${editing.id}`, { body: editing.body.trim() }),
    'Note saved.').then((made) => { if (made) setEditing(null); });
  const removeNote = (id: string) => act('notes', () => postJson(`${base}/notes/${id}/delete`, {}), 'Note removed.');
  const uploadPhoto = async () => {
    if (!file) return;
    const form = new FormData();
    form.append('image_file', file);
    form.append('caption', caption.trim());
    for (const [key, value] of rightsFields(rights)) form.append(key, value);
    const made = await act('photos', () => apiFetch<{ duplicate: boolean }>(`${base}/photos`, { method: 'POST', body: form }),
      (r) => (r.duplicate ? 'This photo is here already.' : 'Photo added.'));
    if (made) {
      setFile(null);
      setCaption('');
      setRights(emptyRights());
      setInputKey((k) => k + 1);
    }
  };
  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragging(false);
    const dropped = e.dataTransfer.files?.[0];
    if (dropped) setFile(dropped);
  };
  const checkPrice = async (redo: string) => {
    setBusy(true);
    setNote(null);
    setEstimate(null);
    try {
      setEstimate({ feedback: redo, result: await postJson<BoardEstimate>(`${base}/board/estimate`, { feedback: redo }) });
    } catch (e) {
      setNote({ tone: 'problem', text: problemText(e), where: 'board' });
    } finally {
      setBusy(false);
    }
  };
  const makeBoard = async () => {
    if (!estimate) return;
    const made = await act('board', () => postJson<{ board: WorldBoard | null; request?: WorldBoardRequest }>(`${base}/board`, {
      feedback: estimate.feedback, approved_total_usd_micros: estimate.result.total_usd_micros,
    }), (r) => {
      if (!r.board) return BOARD_STARTED;
      return estimate.feedback ? 'The new mood board is ready below.' : 'Your mood board is ready below.';
    });
    if (made) {
      setJustMade(made.board?.board_id ?? 'making');
      setEstimate(null);
      setFeedback('');
    }
  };
  const approve = async (board: WorldBoard) => {
    const done = await act('board', () => postJson(`${base}/board/${board.board_id}/approve`, {}), 'Mood board approved. Next: the bible.');
    if (done !== null) router.push(stepHref('bible'));
  };
  const writeBible = async () => {
    const made = await act('bible', () => postJson<{ bible: BibleResult }>(`${base}/bible`, {}), (r) => {
      const c = r.bible.created;
      return `Added ${plural(c.characters.length, 'character')}, ${plural(c.places.length, 'place')} and ${plural(c.props.length, 'prop')}.`;
    });
    if (made) setBible(made.bible);
  };

  const priceOf = (result: BoardEstimate) => (result.dry_run ? 'Free' : formatUsd(result.total_usd_micros));
  const estimateBlock = (forRedo: boolean) => {
    if (!estimate || Boolean(estimate.feedback) !== forRedo) return null;
    const result = estimate.result;
    const budget = result.dry_run ? null : result.budget ?? null;
    return (
      <PriceBlock testId="board-estimate" className="mt-3.5" total={priceOf(result)}
        rows={budget ? [{ label: 'Mood board budget left now', value: formatUsdMicros(budget.available_usd_micros) }] : []}
        after={budget ? { label: 'Left after this board', value: formatUsdMicros(budget.available_usd_micros - result.total_usd_micros) } : null}>
        <p className="mt-2 text-[13px] font-bold">
          {result.dry_run ? `${result.summary} (a live board would cost ${formatUsd(result.total_usd_micros)})` : result.summary}
        </p>
        {!result.dry_run && (
          <>
            <ul className="mt-1.5 list-disc space-y-0.5 pl-5 text-[12.5px]" data-testid="board-estimate-lines">
              {boardEstimateLines(result).map((line) => <li key={line}>{line}</li>)}
            </ul>
            <p className="mt-1.5 text-[12px] text-ink-soft">
              Nothing is charged until you press Make. Before each picture the studio checks its price against
              this estimate, the balance and the budget again, and stops (keeping the pictures already made) if
              one of them changed.
            </p>
          </>
        )}
      </PriceBlock>
    );
  };

  const stops = screenSteps(journey, step).map((s) => ({
    key: s.key,
    name: s.name,
    note: s.key === 'references' && s.complete ? 'Add more any time' : s.note,
    state: s.state,
    complete: s.complete,
    href: s.complete || s.key === journey.current || s.key === step ? stepHref(s.key) : null,
  }));
  const bubble: Record<CreationStep, string> = {
    spark: 'Every Universe starts with a spark: a name and a line about it.',
    references: `The more you show me, the closer the board gets. ${plural(counts.photos, 'photo')} and ${plural(counts.notes, 'note')} so far!`,
    moodboard: making ? 'Pictures appear on the board as they are made.' : `I mix your ${plural(counts.photos, 'photo')} and ${plural(counts.notes, 'note')} into one look.`,
    bible: 'From the approved board I write the characters, places and props.',
    lock: counts.characters === 0 ? 'No characters yet: add them on the Universe page.' : `${counts.locked} of ${plural(counts.characters, 'look')} sealed.`,
  };

  const head = (
    <section className="relative mt-3 sm:mt-6" aria-labelledby="creation-title">
      <p className="pf-bubble absolute right-[46px] top-6 z-[3] hidden max-w-[270px] lg:block" aria-live="polite">{bubble[step]}</p>
      <HeroBand className="px-5 pb-8 pt-6 sm:px-10 sm:pb-10 sm:pt-[26px]">
        <nav aria-label="Breadcrumb" className="text-[13px] font-extrabold text-ink">
          <Link href="/studio" className="text-blue-deep">Universes</Link>
          <span aria-hidden="true"> › </span>
          <Link href={here} className="text-blue-deep" data-testid="universe-name">{world.name}</Link>
        </nav>
        <div className="mt-2 flex flex-wrap gap-2">
          <span className="pf-chip bg-white/80 text-ink">Step {stepNumber(step)} of 5</span>
          {preset && <span className="pf-chip bg-white/80 text-ink" data-testid="universe-genre">{preset.name}</span>}
          <span className={`pf-chip ${healthTone}`} data-testid="universe-health">{healthLine(world.counts, drift)}</span>
        </div>
        <h1 id="creation-title" className="mt-2 text-[30px] sm:text-[40px]">{TITLES[step]}</h1>
        <div className="mx-auto mt-4 max-w-[860px] rounded-card bg-white/70 px-3 pb-1.5 pt-4 backdrop-blur-md sm:px-[22px]">
          <JourneyPath stops={stops} current={step} label="Steps to make this Universe" testId="creation-path" />
        </div>
      </HeroBand>
    </section>
  );

  const referencesSummary = (
    <div>
      <div className="flex flex-wrap items-end justify-between gap-2">
        <h2 className="text-[22px]">Your references</h2>
        <Link href={stepHref('references')} className="text-[13px] font-extrabold text-blue-deep">Add more →</Link>
      </div>
      {creation.photos.length > 0 && (
        <ul className="mt-3 grid grid-cols-3 gap-3 sm:grid-cols-6">
          {creation.photos.slice(0, 6).map((p) => (
            <RefTile key={p.image_id} src={`${API_URL}${p.thumb_url}`} alt={p.caption || 'Reference photo'} />
          ))}
        </ul>
      )}
      {creation.photos.length > 6 && <p className="mt-2 text-[13px] font-bold text-ink-soft">+ {plural(creation.photos.length - 6, 'more photo')}</p>}
      {creation.notes.length > 0 && (
        <div className="mt-4 grid gap-3.5 sm:grid-cols-2 lg:grid-cols-4">
          {creation.notes.slice(0, 4).map((n) => (
            <StickyNote key={n.note_id} colour={TOPIC_STICKY[n.topic] ?? 'yellow'} title={TOPIC_TEXT[n.topic]?.label ?? n.topic}>
              <span className="line-clamp-4 whitespace-pre-line break-words">{n.body}</span>
            </StickyNote>
          ))}
        </div>
      )}
      {counts.notes + counts.photos === 0 && (
        <EmptyState className="mt-3" icon="photo" title="No references yet" action={<Link href={stepHref('references')} className="pf-btn pf-btn-soft pf-btn-sm">Add photos and notes</Link>}>
          The mood board is made from them.
        </EmptyState>
      )}
    </div>
  );

  let main: ReactNode = null;
  let side: ReactNode = null;

  if (step === 'spark') {
    main = (
      <section className="pf-card p-6" data-testid="creation-spark" aria-labelledby="spark-title">
        <h2 id="spark-title" className="text-[22px]">The spark</h2>
        <p className={hint}>Changes save as you type. Nothing here costs anything.</p>
        <p className={`${fieldLabel} mt-4`}>Name</p>
        <p className="font-display text-[22px] font-semibold">{world.name}</p>
        <p className={hint}>Chosen when the Universe started.</p>
        <label htmlFor="creation-logline" className={`${fieldLabel} mt-4`}>What&apos;s it about?</label>
        <textarea id="creation-logline" value={spark?.logline ?? ''} rows={3} maxLength={view.limits.logline_max}
          onChange={(e) => setSpark((s) => ({ logline: e.target.value, preset: s?.preset ?? '' }))}
          placeholder="e.g. A kiln village where every bell remembers a name…" className={field} data-testid="creation-logline" />
        <label htmlFor="creation-genre" className={`${fieldLabel} mt-4`}>Kind of video new episodes start with</label>
        <select id="creation-genre" value={spark?.preset ?? ''} className={field} data-testid="creation-genre"
          onChange={(e) => {
            setSpark((s) => ({ logline: s?.logline ?? '', preset: e.target.value }));
            saveProfile({ preset_id: e.target.value || null });
          }}>
          <option value="">Decide per episode</option>
          {CARD_GROUPS.map((group) => (
            <optgroup key={group.template} label={group.label}>
              {group.presets.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </optgroup>
          ))}
        </select>
        <p className="mt-3 text-[12.5px] font-bold text-ink-soft" role="status" data-testid="creation-spark-saved">
          {sparkSaving ? 'Saving…' : 'Saved'}
        </p>
        <Message note={note} where="spark" />
      </section>
    );
    side = (
      <>
        <h3 className="text-[19px]">Next: references</h3>
        <p className={hint}>Photos and a few notes on the premise, the characters, the places and the tone.</p>
        <SideNext href={stepHref('references')}>Next: References →</SideNext>
      </>
    );
  } else if (step === 'references') {
    main = (
      <div data-testid="creation-references">
        <div className="flex flex-wrap items-end justify-between gap-2">
          <h2 className="text-[22px]">Your references</h2>
          <span className="text-[13px] text-ink-soft">Only you and your episode editors see these.</span>
        </div>
        {creation.photos.length < limits.max_photos && (
          <div data-testid="creation-photo-form" onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)} onDrop={onDrop}
            className={`mt-3 rounded-card border-[3px] border-dashed p-5 ${dragging ? 'border-blue-deep bg-blue-100' : 'border-blue/40 bg-blue-soft'}`}>
            <div className="flex flex-col gap-4 sm:flex-row sm:items-center">
              <div aria-hidden="true" className="grid size-16 flex-none place-items-center rounded-[20px] bg-white text-blue-deep">
                <Icon name="photo" size={32} />
              </div>
              <div className="min-w-0 flex-1">
                <h3 className="text-[19px]">Drop photos, sketches or screenshots</h3>
                <p id="creation-photo-hint" className={hint}>
                  Up to {limits.max_photos}. PNG, JPG or WebP, at least {view.limits.min_image_px} px on each side, up to{' '}
                  {megabytes(view.limits.max_upload_bytes)} MB. Your own, or ones you have the rights to.
                </p>
              </div>
              <label htmlFor="creation-photo-file" className="pf-btn pf-btn-primary pf-btn-sm cursor-pointer focus-within:outline-3 focus-within:outline-blue-deep">
                Choose a photo
                <input id="creation-photo-file" key={inputKey} type="file" accept={PICTURE_ACCEPT} aria-describedby="creation-photo-hint"
                  onChange={(e) => setFile(e.target.files?.[0] ?? null)} className="sr-only" data-testid="creation-photo-file" />
              </label>
            </div>
            {file && <p className="mt-3 break-words text-[13px] font-bold">Chosen: {file.name}</p>}
            {fileIssue && <p className="mt-1 text-[12.5px] font-bold text-coral-ink">{fileIssue}</p>}
            <input value={caption} onChange={(e) => setCaption(e.target.value)} maxLength={limits.caption_max}
              aria-label="Caption (optional)" placeholder="Caption (optional), e.g. the harbour at dusk" className={`${field} mt-3`}
              data-testid="creation-photo-caption" />
            {file && <RightsQuestions rights={rights} onChange={setRights} group="creation-rights" testId={(w) => `creation-${w}`} />}
            {file && !fileIssue && rightsIssue && <p className="mt-2 text-[12.5px] font-bold text-mustard-ink">{rightsIssue}</p>}
            <button type="button" className="pf-btn pf-btn-primary pf-btn-sm mt-3" disabled={busy || !file || !!fileIssue || !!rightsIssue}
              title={file ? undefined : 'Turns on once you choose a photo.'} aria-describedby={file ? undefined : 'creation-photo-why'}
              onClick={uploadPhoto} data-testid="creation-photo-send">
              Upload the photo
            </button>
            {!file && <p id="creation-photo-why" className="pf-off-why" data-testid="creation-photo-why">Turns on once you choose a photo.</p>}
          </div>
        )}
        <Message note={note} where="photos" />
        {creation.photos.length > 0 && (
          <ul className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-6" data-testid="creation-photos">
            {creation.photos.map((p) => (
              <RefTile key={p.image_id} testId={`creation-photo-${p.image_id}`} src={`${API_URL}${p.thumb_url}`}
                href={`${API_URL}${p.url}`} alt={p.caption || 'Reference photo'} caption={p.caption}>
                <ConfirmButton question="Remove this photo?" confirmLabel="Remove" disabled={busy} className="pf-btn pf-btn-quiet pf-btn-sm w-full"
                  onConfirm={() => act('photos', () => postJson(`${base}/photos/${p.image_id}/delete`, {}), 'Photo removed.')}>
                  Remove
                </ConfirmButton>
              </RefTile>
            ))}
          </ul>
        )}

        <h2 className="mt-7 text-[22px]">Your notes</h2>
        {creation.notes.length < limits.max_notes && (
          <div className="pf-card mt-3 p-4">
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
              <label htmlFor="creation-topic" className="text-[13px] font-extrabold">About</label>
              <select id="creation-topic" value={topic} onChange={(e) => setTopic(e.target.value)} className={`${field} sm:w-auto`}
                data-testid="creation-topic">
                {creation.topics.map((t) => <option key={t} value={t}>{TOPIC_TEXT[t]?.label ?? t}</option>)}
              </select>
            </div>
            <textarea value={text} onChange={(e) => setText(e.target.value)} maxLength={limits.note_max} rows={3}
              aria-label="Note" placeholder={TOPIC_TEXT[topic]?.placeholder} className={`${field} mt-2`}
              data-testid="creation-note-text" />
            <div className="mt-2 flex flex-wrap items-center gap-3">
              <button type="button" className="pf-btn pf-btn-soft pf-btn-sm" disabled={busy || !text.trim()} onClick={addNote}
                title={text.trim() ? undefined : 'Turns on once you write a note.'} aria-describedby="creation-note-why"
                data-testid="creation-note-add">
                <Icon name="plus" size={13} />Add the note
              </button>
              <span id="creation-note-why" className={text.trim() ? hint : 'pf-off-why !mt-0'} data-testid="creation-note-why">
                {text.trim() ? 'Kept on this device until you add it.' : 'Turns on once you write a note.'}
              </span>
            </div>
          </div>
        )}
        <Message note={note} where="notes" />
        {creation.notes.length > 0 && (
          <ul className="mt-4 grid gap-3.5 sm:grid-cols-2 lg:grid-cols-4" data-testid="creation-notes">
            {creation.notes.map((n) => (
              <li key={n.note_id} data-testid={`creation-note-${n.note_id}`}>
                <StickyNote colour={TOPIC_STICKY[n.topic] ?? 'yellow'} title={TOPIC_TEXT[n.topic]?.label ?? n.topic}>
                  {editing?.id === n.note_id ? (
                    <div className="space-y-2">
                      <textarea value={editing.body} onChange={(e) => setEditing({ id: n.note_id, body: e.target.value })}
                        maxLength={limits.note_max} rows={4} aria-label="Note" className={field} />
                      <div className="flex flex-wrap gap-2">
                        <button type="button" className="pf-btn pf-btn-primary pf-btn-sm" disabled={busy || !editing.body.trim()} onClick={saveNote}
                          title={editing.body.trim() ? undefined : 'A note can’t be empty.'}>Save</button>
                        <button type="button" className="pf-btn pf-btn-quiet pf-btn-sm" onClick={() => setEditing(null)}>Cancel</button>
                      </div>
                    </div>
                  ) : (
                    <>
                      <span className="block whitespace-pre-line break-words">{n.body}</span>
                      <span className="mt-2 flex flex-wrap gap-1.5">
                        <button type="button" className="pf-btn pf-btn-quiet pf-btn-sm !min-h-9 !px-3" disabled={busy}
                          onClick={() => setEditing({ id: n.note_id, body: n.body })}>
                          Edit
                        </button>
                        <ConfirmButton question="Remove this note?" confirmLabel="Remove" disabled={busy} className="pf-btn pf-btn-quiet pf-btn-sm !min-h-9 !px-3"
                          onConfirm={() => removeNote(n.note_id)}>
                          Remove
                        </ConfirmButton>
                      </span>
                    </>
                  )}
                </StickyNote>
              </li>
            ))}
          </ul>
        )}
      </div>
    );
    side = (
      <>
        <h3 className="text-[19px]">{journey.boardOpen ? 'Ready for a mood board' : 'Gathering references'}</h3>
        <p className={hint}>
          {journey.boardOpen
            ? `${creation.board.image_count} pictures will mix your photos and notes into one look. You see the price first.`
            : 'The mood board mixes your photos and notes into one look.'}
        </p>
        <ul className="mt-3 space-y-1 text-[13px] font-bold">
          <li className="flex justify-between border-b border-dashed border-ink/10 py-1.5">Photos<b>{counts.photos} of {limits.max_photos}</b></li>
          <li className="flex justify-between py-1.5">Notes<b>{counts.notes} of {limits.max_notes}</b></li>
        </ul>
        <SideNext href={stepHref('moodboard')} disabled={!journey.boardOpen} reason={unlockHint}>Next: Mood board →</SideNext>
      </>
    );
  } else if (step === 'moodboard') {
    const showDecide = Boolean(waiting) && !making;
    main = latest ? (
      <section className="pf-card p-5 sm:p-6" data-testid="creation-board" aria-labelledby="board-title">
        <div className="mb-4 flex flex-wrap items-start justify-between gap-3" data-testid="board-latest" data-status={latest.status}>
          <div>
            <h2 id="board-title" className="text-[22px]">Mood board {latest.version}</h2>
            <p className={hint}>
              Made {formatLocalDate(latest.created_at)} · {latest.dry_run ? 'free stand-in pictures (dry run)' : `priced at ${formatUsdMicros(latest.estimate_usd_micros)} before it ran`}
              {latest.answers_feedback && <> · asked: {latest.answers_feedback}</>}
            </p>
          </div>
          {latest.status === 'approved' && <span className="pf-chip pf-chip-lock"><Icon name="check" size={11} />Board {latest.version} approved</span>}
        </div>
        <div className="relative">
          <BoardPictures board={latest} dropping={justMade === latest.board_id} testId={`board-images-${latest.version}`} />
          {latest.status === 'approved' && <span className="pf-stamp right-6 top-6" aria-hidden="true">APPROVED</span>}
        </div>
        {progress?.kind === 'short' && creation.board_request?.board_id === latest.board_id && (
          <p className={`${hint} mt-3`} data-testid="board-short">{progress.text}</p>
        )}
        {showDecide && waiting && (
          <div className="mt-5 grid grid-cols-1 gap-5 lg:grid-cols-[1fr_1.25fr]" data-testid="creation-approve">
            <div className="rounded-card bg-green-soft p-5" data-testid="board-love-it">
              <h3 className="text-[19px]">Love it?</h3>
              <p className="mb-3 mt-1 text-[13.5px]">
                Approving pins this board as the look of {world.name}. Next we write the character and setting bible from it.
              </p>
              <button type="button" className="pf-btn pf-btn-go" disabled={busy} onClick={() => approve(waiting)} data-testid="board-approve">
                <Icon name="check" size={16} />Approve board {waiting.version} → write the bible
              </button>
            </div>
            {!creation.board.unavailable && (
              <div className="rounded-card bg-mustard-soft p-5" data-testid="board-not-quite">
                <h3 className="text-[19px]">Not quite?</h3>
                <label htmlFor="board-feedback" className="mb-2 mt-1 block text-[13.5px]">
                  Tell us what to change. We show the price of the redo before it runs.
                </label>
                <textarea id="board-feedback" value={feedback} onChange={(e) => setFeedback(e.target.value)} rows={2}
                  maxLength={limits.feedback_max} placeholder="e.g. warmer light, fewer people, more of the sea"
                  className={field} data-testid="board-feedback" />
                <button type="button" className="pf-btn pf-btn-ghost pf-btn-sm mt-2.5" disabled={busy || !feedback.trim()}
                  title={feedback.trim() ? undefined : 'Turns on once you say what to change.'}
                  aria-describedby={feedback.trim() ? undefined : 'board-redo-why'}
                  onClick={() => checkPrice(feedback.trim())} data-testid="board-redo-price">
                  Check the price of a redo
                </button>
                {!feedback.trim() && (
                  <p id="board-redo-why" className="pf-off-why" data-testid="board-redo-why">Turns on once you say what to change.</p>
                )}
                {estimateBlock(true)}
                {estimate?.feedback && (
                  <div className="mt-3 flex flex-wrap gap-2">
                    <button type="button" className="pf-btn pf-btn-go" disabled={busy} onClick={makeBoard} data-testid="board-make">
                      <Icon name="spark" size={16} />Make the new mood board · {priceOf(estimate.result)}
                    </button>
                    <button type="button" className="pf-btn pf-btn-quiet" disabled={busy} onClick={() => setEstimate(null)}>Cancel</button>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </section>
    ) : <div data-testid="creation-board">{referencesSummary}</div>;

    const sideStatus = (
      <>
        {progress?.kind === 'making' && (
          <div className="mt-3 rounded-inner bg-aqua-soft p-3" data-testid="board-making" role="status">
            <p className="text-[13.5px] font-bold text-aqua-ink">{progress.text}</p>
            <progress className="mt-2 h-2 w-full accent-aqua-deep" max={progress.asked} value={progress.made}
              aria-label="Mood board pictures made" />
          </div>
        )}
        {progress?.kind === 'failed' && (
          <p className="mt-3 rounded-inner bg-coral-soft p-3 text-[13.5px] font-bold text-coral-ink" data-testid="board-failed" role="alert">
            {progress.text}
          </p>
        )}
        <Message note={note?.text === BOARD_STARTED && !making ? null : note} where="board" />
      </>
    );
    const priced = Boolean(estimate && !estimate.feedback);
    if (!latest) {
      side = (
        <>
          <h3 className="text-[19px]">{making ? 'Making your mood board' : 'Ready for a mood board'}</h3>
          <p className={hint}>We make {creation.board.image_count} pictures that mix your photos and notes into one look.</p>
          {sideStatus}
          {creation.board.unavailable && !making ? (
            <p className="mt-3 rounded-inner bg-mustard-soft p-3 text-[13.5px] font-bold text-mustard-ink" data-testid="board-unavailable">
              {creation.board.unavailable}
            </p>
          ) : !making && (
            <>
              <button type="button" className="pf-btn pf-btn-soft mt-3.5 w-full" disabled={busy || !journey.boardOpen}
                title={unlockHint ?? undefined} aria-describedby={unlockHint ? 'board-unlock-why' : undefined}
                onClick={() => checkPrice('')} data-testid="board-check-price">
                Check the price
              </button>
              {unlockHint && <p id="board-unlock-why" className="pf-off-why text-center">{unlockHint}</p>}
              {estimateBlock(false)}
              <button type="button" className="pf-btn pf-btn-go mt-4 w-full" disabled={busy || !estimate || Boolean(estimate.feedback)}
                title={priced ? undefined : 'Turns on once the price is on screen.'} aria-describedby="board-make-why"
                onClick={makeBoard} data-testid="board-make">
                <Icon name="spark" size={16} />
                {estimate && !estimate.feedback ? `Make my mood board · ${priceOf(estimate.result)}` : 'Make my mood board'}
              </button>
              {estimate && !estimate.feedback && (
                <button type="button" className="pf-btn pf-btn-quiet pf-btn-sm mt-2 w-full" disabled={busy} onClick={() => setEstimate(null)}>
                  Cancel
                </button>
              )}
              <p id="board-make-why" className={`${priced ? 'mt-2 text-[12px] text-ink-soft' : 'pf-off-why'} text-center`} data-testid="board-make-why">
                {priced
                  ? 'Nothing is spent until you press this.'
                  : 'Turns on once the price is on screen. If we can’t get a price, it stays off: nothing is spent until you press it.'}
              </p>
            </>
          )}
          <ul className="mt-3 space-y-1 text-[12.5px]">
            {[`Uses your ${plural(counts.photos, 'photo')} and ${plural(counts.notes, 'note')}`,
              'Redo as many times as you like, each priced first', 'Nothing else runs until you approve'].map((line) => (
              <li key={line} className="flex gap-2"><span aria-hidden="true" className="font-black text-green-ink">✓</span>{line}</li>
            ))}
          </ul>
        </>
      );
    } else {
      side = (
        <>
          <h3 className="text-[19px]">{approved ? `Board ${approved.version} approved` : making ? 'Making the new board' : 'Look it over'}</h3>
          {approved ? (
            <p className="mt-1 text-[13.5px]" data-testid="board-approved">
              Mood board {approved.version} is approved: it sets the look of this Universe.
            </p>
          ) : (
            <p className={hint}>Approve it, or say what to change and see the price of a redo first. Nothing else runs until you approve.</p>
          )}
          {sideStatus}
          {approved && <SideNext href={stepHref('bible')}>Next: write the bible →</SideNext>}
        </>
      );
    }
  } else if (step === 'bible') {
    const profile = world.profile;
    const premise = creation.notes.find((n) => n.topic === 'premise')?.body || profile.logline || '';
    const tone = toneChips(creation.notes.filter((n) => n.topic === 'tone').map((n) => n.body).join(', '));
    const toneColours = [tones.warn, tones.run, tones.bad, tones.ok, tones.info, tones.wait];
    main = (
      <div data-testid="creation-bible">
        {!approved ? (
          <EmptyState title="Approve a mood board first" icon="book"
            action={<Link href={stepHref('moodboard')} className="pf-btn pf-btn-soft pf-btn-sm">Go to the mood board</Link>}>
            The bible is written from the approved board and your notes.
          </EmptyState>
        ) : !bibleDone ? (
          <section className="pf-card p-6" aria-labelledby="bible-title">
            <h2 id="bible-title" className="text-[22px]">Characters, places and props</h2>
            <p className="mt-1 text-[13.5px]" data-testid="board-approved">
              Mood board {approved.version} is approved: it sets the look of this Universe.
            </p>
            <p className={`${hint} mt-2`}>
              The studio writes the characters, places and props from the approved board and your notes, and fills an empty
              story bible and style notes. Names you have already stay as they are.
            </p>
            <div className="mt-4"><BoardPictures board={approved} dropping={false} /></div>
            <button type="button" className="pf-btn pf-btn-go mt-5" disabled={busy} onClick={writeBible} data-testid="bible-write">
              <Icon name="book" size={16} />{busy ? 'Writing…' : 'Write the bible'}
            </button>
          </section>
        ) : (
          <section className="pf-card p-6" aria-labelledby="bible-title">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 id="bible-title" className="text-[22px]">Story bible</h2>
              <Link href={`${here}#bible`} className="text-[13px] font-extrabold text-blue-deep">Edit on the Universe page</Link>
            </div>
            <p className={`${hint} mt-1`} data-testid="bible-done">
              Written from mood board {approved.version}. Change them on the Universe page; lock each character&apos;s look from an episode.
            </p>
            {premise && (<><h3 className="mb-1.5 mt-4 text-[15px]">Premise</h3><p className="whitespace-pre-line break-words text-[13.5px]">{premise}</p></>)}
            {tone.length > 0 && (
              <>
                <h3 className="mb-1.5 mt-4 text-[15px]">Tone</h3>
                <div className="flex flex-wrap gap-1.5">
                  {tone.map((t, i) => <span key={t} className={`pf-chip ${toneColours[i % toneColours.length]}`}>{t}</span>)}
                </div>
              </>
            )}
            {profile.bible && (
              <>
                <h3 className="mb-1.5 mt-4 text-[15px]">How this Universe works</h3>
                <div className="space-y-2 text-[13.5px]">
                  {profile.bible.split(/\n{2,}/).map((para, i) => (
                    <p key={i} className="flex gap-2 whitespace-pre-line break-words"><span aria-hidden="true" className="text-coral">✦</span><span>{para}</span></p>
                  ))}
                </div>
              </>
            )}
            {profile.style_notes && (<><h3 className="mb-1.5 mt-4 text-[15px]">Style</h3><p className="whitespace-pre-line break-words text-[13.5px]">{profile.style_notes}</p></>)}
            <h3 className="mb-2 mt-5 text-[15px]">Who and what it keeps</h3>
            <ul className="flex flex-wrap gap-2">
              {view.characters.map((c) => (
                <li key={c.character_id} className="inline-flex items-center gap-1.5 rounded-full bg-white px-3 py-1.5 text-[13px] font-extrabold shadow-card">
                  <CastDot colour={colours[c.slug]} />{c.name}
                </li>
              ))}
              {view.items.map((item) => (
                <li key={item.prop_id} className={`pf-chip ${item.kind === 'place' ? 'pf-chip-blue' : 'pf-chip-lock'} !py-1.5`}>
                  {item.kind === 'place' ? 'Place' : 'Prop'}: {item.name}
                </li>
              ))}
            </ul>
          </section>
        )}
        {bible && (bible.skipped.length > 0 || bible.dropped.length > 0) && (
          <p className={`${hint} mt-3`} data-testid="bible-left-out">
            {bible.skipped.length > 0 && <>Kept as they were: {bible.skipped.join(', ')}. </>}
            {bible.dropped.length > 0 && <>Left out (the studio cannot make them): {bible.dropped.join(', ')}.</>}
          </p>
        )}
        <Message note={note} where="bible" />
      </div>
    );
    side = (
      <>
        <h3 className="text-[19px]">{bibleDone ? 'Bible written' : 'The bible'}</h3>
        <p className={hint}>
          {bibleDone
            ? `${plural(counts.characters, 'character')} and ${counts.things} places and props joined ${world.name}. Add photos and voices on the Universe page.`
            : 'Characters, places and props, written once from the approved board. You can change every word afterwards.'}
        </p>
        <SideNext href={stepHref('lock')} disabled={!bibleDone} reason="Turns on once the bible is written.">
          Next: lock the looks →
        </SideNext>
        <Link href={here} className="pf-btn pf-btn-quiet mt-2 w-full" data-testid="creation-open-universe">Open the Universe</Link>
      </>
    );
  } else {
    main = (
      <section className="pf-card p-6" data-testid="creation-lock" aria-labelledby="lock-title">
        <h2 id="lock-title" className="text-[22px]">Seal each look</h2>
        <p className="mt-1 text-[13.5px]">
          A look is locked from an episode in this Universe: make the character sheet there and lock it. Every episode after
          uses exactly that sheet and those photos, and an episode starts once every character in it is locked.
        </p>
        {view.characters.length === 0 ? (
          <EmptyState className="mt-4" icon="user" title="No characters yet"
            action={<Link href={`${here}#characters`} className="pf-btn pf-btn-soft pf-btn-sm">Add a character</Link>}>
            Write the bible, or add characters on the Universe page.
          </EmptyState>
        ) : (
          <ul className="mt-4 grid gap-3 sm:grid-cols-2">
            {view.characters.map((c) => {
              const seal = sealFor({ characters: 1, locked_characters: c.locked_versions.length > 0 ? 1 : 0 });
              return (
                <li key={c.character_id} className="flex items-center gap-3 rounded-inner bg-white p-3 shadow-card" data-testid={`creation-look-${c.slug}`}>
                  <Seal state={seal.state} top={c.locked_versions.length > 0 ? 'locked' : 'not'} bottom={c.locked_versions.length > 0 ? `v${Math.max(...c.locked_versions)}` : 'locked'} size={54} />
                  <div className="min-w-0">
                    <p className="flex items-center gap-1.5 font-extrabold"><CastDot colour={colours[c.slug]} />{c.name}</p>
                    <p className={hint}>{c.locked_versions.length > 0 ? 'Look locked' : 'Not locked yet'}</p>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    );
    side = (
      <>
        <h3 className="text-[19px]">{journey.current === null ? 'Every look is sealed' : 'Lock from an episode'}</h3>
        <p className={hint}>{counts.locked} of {plural(counts.characters, 'look')} locked. Locking itself is free; a character sheet is priced before it is made.</p>
        {lockEpisode ? (
          <Link href={`/studio/episodes/${encodeURIComponent(lockEpisode)}#cast`} className="pf-btn pf-btn-primary mt-4 w-full" data-testid="creation-lock-episode">
            Open {lockEpisode} to lock looks
          </Link>
        ) : (
          <Link href={`/studio/create?world=${encodeURIComponent(seriesId)}`} className="pf-btn pf-btn-primary mt-4 w-full" data-testid="creation-first-episode">
            Make your first episode
          </Link>
        )}
        <Link href={here} className="pf-btn pf-btn-quiet mt-2 w-full" data-testid="creation-open-universe">Open the Universe</Link>
      </>
    );
  }

  return (
    <div className="mx-auto max-w-[1240px] px-4 pb-14 sm:px-8" data-testid="universe-creation" data-step={step}>
      {head}
      <div className="mt-4 lg:hidden" data-testid="creation-tip">
        <p className="pf-bubble" data-testid="creation-bubble">{bubble[step]}</p>
      </div>
      <div className="mt-6 grid gap-6 lg:grid-cols-[1fr_380px]">
        <div className="min-w-0" data-testid={`creation-step-${stepNumber(step)}`}>{main}</div>
        <GlassPanel as="aside" className="self-start p-[22px] lg:sticky lg:top-24" aria-label={`${CREATION_NAMES[step]}: what's next`}>
          {side}
        </GlassPanel>
      </div>
    </div>
  );
}

export default UniverseCreation;
