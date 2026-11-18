'use client';

import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { API_URL, ApiError, apiFetch, postJson } from '@/lib/studio-api';
import { balanceDisplay, parseBalanceInput } from '@/lib/balance.mjs';
import type { HiggsfieldBalance } from '@/lib/balance.mjs';
import { keepPolling, runEndedView } from '@/lib/run-outcome.mjs';
import type { RunInfo } from '@/lib/run-outcome.mjs';
import { finishLine, gateChip, openGate } from '@/lib/next-step.mjs';
import {
  REDO_NOTE_MAX, redoEstimateRequest, redoNote, redoNoteText, redoPriceText, redoRequest, redoStartedText,
} from '@/lib/redo.mjs';
import type { RedoEstimate } from '@/lib/redo.mjs';
import { FILE_LABELS, fileLabel, gateLabel, qcLabel, stageLabel, stillChip } from '@/lib/labels';
import { formatLocalDateTime } from '@/lib/time';
import { btnDanger, btnPrimary, btnSecondary, card, chip, focusRing, highlight, input, tones } from '@/lib/ui';
import { ConfirmButton } from './ConfirmButton';
import ShotPlanEditor from './ShotPlanEditor';

/**
 * The episode run, section by section (G-UI-1): the shot plan (G1), the scene pictures (G2, per-picture
 * decisions), the final cut (G_FINAL_CUT, clip redos, approve or send back), the finished video and its
 * downloads, and, under Production details, the raw run state, credit cap, moderation ladder, QC verdicts
 * and per-shot workflow state. Every action is an existing API route; the API (and behind it the
 * workflow) stays the authority, so a refused action shows the API's reason instead of pretending it
 * worked. The page (app/studio/episodes/[id]/page.tsx) lays the sections out and owns the actions.
 */

interface QcView { verdict?: string; blocking?: boolean; reason?: string; checks?: unknown }
interface StageView {
  qc?: QcView;
  acceptance?: { reason?: string } | null;
  blocking?: boolean;
  stale?: boolean;
  edit_version?: number;
  due?: boolean;
  phase?: string | null;
}
export interface RunState {
  episode_id: string;
  stage: string | null;
  current_gate: string | null;
  passed_gates: string[];
  approvals: Record<string, boolean>;
  shots_count: number;
  shot_decisions: Record<string, string>;
  rejection_reasons: Record<string, string>; // empty for a viewer who may not see this episode's cast
  pending_retries: string[];
  clip_retries: Record<string, number>;
  pending_clip_retries: string[];
  final_cut_refusals: { reason?: string }[] | string[];
  final_cut_rejections: { reason?: string }[];
  failed_shots: (string | { shot_id?: string; status?: string; reason?: string })[];
  shot_outcomes: Record<string, { status?: string; still_url?: string | null; clip_url?: string | null }>;
  active_stall_alerts: number;
  assembly: StageView | null;
  delivery: StageView | null;
  archetype_id: string | null;
  run: RunInfo | null;
}
export interface ShotState {
  available: boolean;
  still_status?: string;
  clip_status?: string;
  still_error?: string | null;
  clip_error?: string | null;
  moderation?: Record<string, { steps: string[]; retry: unknown }>;
  moderation_escalation?: unknown;
  pre_approved?: boolean;
  reason?: string;
}
/** A row of GET /api/episodes/{id}/shots (the studio database). */
export interface ShotRow {
  id: string;
  shot_id: string;
  status: string;
  still_url: string | null;
  clip_url: string | null;
  prompt: string | null; // null for a member who may not see this episode's cast
  retries: number;
  pre_approved?: boolean;
  beat_id?: string;
  beat_purpose?: string;
}
interface Credits {
  cap_credits: number; spent_credits: number; reserved_credits: number; available_credits: number;
  pause_threshold: number; pause_needed: boolean; over_cap: boolean;
  higgsfield_balance?: HiggsfieldBalance | null;
}
interface StallAlert { job_id: string; shot_id: string | null; job_type: string; minutes_stalled: number; status: string }
export interface FileInfo { available: boolean; size_bytes?: number; url?: string; reason?: string }
export interface ShotPreflight { ok: boolean; errors: string[]; warnings: string[] }

/** Runs an action: the result shows as a notice, then everything reloads. False when it was refused. */
export type Act = (fn: () => Promise<unknown>, done: string, failed: string) => Promise<boolean>;

const UNREACHABLE = 'The studio could not be reached. Check your connection and try again.';

export function problemText(e: unknown): string {
  return e instanceof ApiError ? e.message : UNREACHABLE;
}

/** Older API builds omit the run-control fields: default them instead of crashing the page. */
function normalizeRunState(episodeId: string, raw: Partial<RunState>): RunState {
  return {
    episode_id: raw.episode_id ?? episodeId,
    stage: raw.stage ?? null,
    current_gate: raw.current_gate ?? null,
    passed_gates: raw.passed_gates ?? [],
    approvals: raw.approvals ?? {},
    shots_count: raw.shots_count ?? 0,
    shot_decisions: raw.shot_decisions ?? {},
    rejection_reasons: raw.rejection_reasons ?? {},
    pending_retries: raw.pending_retries ?? [],
    clip_retries: raw.clip_retries ?? {},
    pending_clip_retries: raw.pending_clip_retries ?? [],
    final_cut_refusals: raw.final_cut_refusals ?? [],
    final_cut_rejections: raw.final_cut_rejections ?? [],
    failed_shots: raw.failed_shots ?? [],
    shot_outcomes: raw.shot_outcomes ?? {},
    active_stall_alerts: raw.active_stall_alerts ?? 0,
    assembly: raw.assembly ?? null,
    delivery: raw.delivery ?? null,
    archetype_id: raw.archetype_id ?? null,
    run: raw.run ?? null,
  };
}

export type RunLoad = 'loading' | 'ok' | 'missing' | 'error';

/**
 * The run behind the episode page, polled every 3 s until it has ended: the workflow state (404 = no
 * run yet), the per-shot states, credits, stall alerts and delivery files. The delivery files are read
 * whatever the run state says, a 404 or an error included. An archived run (its history purged, the
 * state rebuilt from what was saved) has no workflow left for shot states or stalls. A failed
 * shot-state read is reported (shotStatesError), never shown as "no shots".
 */
export function useEpisodeRun(episodeId: string) {
  const [state, setState] = useState<RunState | null>(null);
  const [status, setStatus] = useState<RunLoad>('loading');
  const [stateError, setStateError] = useState<string | null>(null);
  const [shotStates, setShotStates] = useState<Record<string, ShotState> | null>(null);
  const [shotStatesError, setShotStatesError] = useState<string | null>(null);
  const [castHidden, setCastHidden] = useState(false);
  const [credits, setCredits] = useState<Credits | null>(null);
  const [stalls, setStalls] = useState<StallAlert[]>([]);
  const [files, setFiles] = useState<Record<string, FileInfo> | null>(null);

  const refresh = useCallback(async () => {
    let read: RunState | null = null;
    try {
      read = normalizeRunState(episodeId, await apiFetch<Partial<RunState>>(`/api/episodes/${episodeId}`));
      setState(read);
      setStatus('ok');
      setStateError(null);
    } catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.status === 400)) {
        setState(null);
        setStatus('missing');
        setStateError(null);
      } else {
        setStatus((prev) => (prev === 'ok' ? 'ok' : 'error'));
        setStateError(problemText(e));
      }
    }
    const workflow = Boolean(read) && !read?.run?.archived;
    const [ss, cr, st, fl] = await Promise.allSettled([
      workflow ? apiFetch<{ shots: Record<string, ShotState>; cast_hidden?: boolean }>(`/api/episodes/${episodeId}/shot-states`) : null,
      read ? apiFetch<Credits>(`/api/episodes/${episodeId}/credits`) : null,
      workflow ? apiFetch<{ alerts: StallAlert[] }>(`/api/episodes/${episodeId}/stalls`) : null,
      apiFetch<{ files: Record<string, FileInfo> }>(`/api/episodes/${episodeId}/files`),
    ]);
    if (ss.status === 'fulfilled') {
      if (ss.value) {
        setShotStates(ss.value.shots ?? {});
        setCastHidden(Boolean(ss.value.cast_hidden));
      }
      setShotStatesError(null);
    } else {
      setShotStatesError(problemText(ss.reason));
    }
    if (cr.status === 'fulfilled' && cr.value && typeof cr.value.cap_credits === 'number') setCredits(cr.value);
    if (st.status === 'fulfilled' && Array.isArray(st.value?.alerts)) setStalls(st.value.alerts.filter((a) => a.status !== 'resolved'));
    if (fl.status === 'fulfilled' && fl.value?.files) setFiles(fl.value.files);
  }, [episodeId]);

  const polling = keepPolling(state);
  useEffect(() => {
    refresh();
  }, [refresh]);
  useEffect(() => {
    if (!polling) return;
    const t = setInterval(refresh, 3000);
    return () => clearInterval(t);
  }, [refresh, polling]);

  return { state, status, stateError, shotStates, shotStatesError, castHidden, credits, stalls, files, refresh, polling };
}

export function fileUrl(episodeId: string, type: string): string {
  return `${API_URL}/api/episodes/${encodeURIComponent(episodeId)}/files/${type}`;
}

/** A remote still is shown as-is; a local one (dry-run placeholder, pre-approved copy) through the API. */
export function stillSrc(episodeId: string, shotId: string, row?: ShotRow | null): string {
  const url = row?.still_url || '';
  if (/^https?:\/\//.test(url)) return url;
  return `${API_URL}/api/episodes/${encodeURIComponent(episodeId)}/shots/${encodeURIComponent(shotId)}/still`;
}

function refusalText(r: unknown): string {
  if (typeof r === 'string') return r;
  if (r && typeof r === 'object' && 'reason' in r) return String((r as { reason: unknown }).reason);
  return JSON.stringify(r);
}

function failedShotIds(state: RunState): string[] {
  return state.failed_shots.map((f) => (typeof f === 'string' ? f : f.shot_id ?? '')).filter(Boolean);
}

/** Every shot the run knows: the shot states, the recorded outcomes and decisions. */
export function runShotIds(state: RunState | null, shotStates: Record<string, ShotState> | null): string[] {
  if (!state) return [];
  return Array.from(new Set([
    ...Object.keys(shotStates ?? {}), ...Object.keys(state.shot_outcomes), ...Object.keys(state.shot_decisions),
    ...failedShotIds(state),
  ])).sort();
}

function SectionTitle({ id, title, status }: { id: string; title: string; status?: { label: string; tone: keyof typeof tones } | null }) {
  return (
    <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h2 id={id} className="text-xl font-semibold text-gray-900">{title}</h2>
      {status && <span className={`${chip} ${tones[status.tone]}`} data-testid={`${id}-chip`}>{status.label}</span>}
    </div>
  );
}

function NotYours() {
  return (
    <p className="text-sm text-gray-700">
      Only the person who made this video, or an admin, can approve or change it.
    </p>
  );
}

// ---------------------------------------------------------------- the top of the page

/**
 * The video to watch and its downloads, in place of the next step: for a completed run, and for a run
 * whose history expired (or could not be read) with whatever files it delivered.
 */
export function FinishLine({ episodeId, run, files, mine = true, children }: {
  episodeId: string; run: RunInfo | null; files: Record<string, FileInfo> | null; mine?: boolean; children?: ReactNode;
}) {
  const finish = finishLine(run, files, mine);
  const done = run?.outcome === 'completed';
  const downloads = Object.keys(FILE_LABELS).filter((type) => files?.[type]?.available);
  return (
    <section id="finished" className={highlight} data-testid="run-ended" data-tone={done ? 'done' : 'expired'}
      aria-labelledby="finished-title">
      {children}
      <h2 id="finished-title" className={`text-2xl font-bold text-gray-900 ${children ? 'mt-4' : ''}`}
        data-testid="run-ended-title">
        {finish.title}
      </h2>
      <p className="mt-1 text-gray-700">{finish.text}</p>
      {!done && run?.reason && (
        <p className="mt-1 break-words text-sm text-gray-700" data-testid="run-ended-reason">{run.reason}</p>
      )}
      {finish.primary && (
        <video data-testid="ready-video" controls playsInline preload="metadata" src={fileUrl(episodeId, finish.primary)}
          aria-label={fileLabel(finish.primary)}
          className="mt-4 max-h-[70vh] w-full max-w-sm rounded-lg bg-black" />
      )}
      {downloads.length > 0 && (
        <div className="mt-4 flex flex-wrap gap-3" data-testid="downloads">
          {downloads.map((type) => (
            <a key={type} data-testid={`download-${type}`} href={`${fileUrl(episodeId, type)}?download=true`}
              className={type === finish.primary ? btnPrimary : btnSecondary}>
              Download: {fileLabel(type)}
            </a>
          ))}
        </div>
      )}
    </section>
  );
}

/** A run that stopped (failed, terminated, canceled, timed out): why, and Start again when allowed. */
export function RunEndedCard({ episodeId, state, busy, act, canAct }: {
  episodeId: string; state: RunState; busy: boolean; act: Act; canAct: boolean;
}) {
  const ended = runEndedView(state.run);
  if (!ended || ended.tone !== 'error') return null;
  return (
    <section id="run-ended" role="alert" data-testid="run-ended" data-tone={ended.tone}
      className="rounded-lg border-2 border-red-300 bg-red-50 p-4 text-red-900 sm:p-6">
      <h2 className="text-xl font-bold" data-testid="run-ended-title">{ended.title}</h2>
      {ended.reason && <p className="mt-1 break-words text-sm" data-testid="run-ended-reason">{ended.reason}</p>}
      {ended.canStartAgain && canAct && (
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <ConfirmButton testId="start-again" question={ended.confirm} confirmLabel="Start again" disabled={busy}
            className={btnPrimary}
            onConfirm={() => act(() => postJson(`/api/episodes/${episodeId}/start-again`, {}), 'Started again.', "Couldn't start again")}>
            Start again
          </ConfirmButton>
          <span className="text-sm">A new run with the same inputs. This one takes no more actions.</span>
        </div>
      )}
    </section>
  );
}

export function StallBanner({ state, stalls }: { state: RunState | null; stalls: StallAlert[] }) {
  const count = Math.max(stalls.length, state?.active_stall_alerts ?? 0);
  if (!count || state?.run?.ended) return null;
  return (
    <div role="status" data-testid="stall-banner"
      className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-amber-900">
      <p className="font-semibold">
        {count === 1 ? 'One step is taking longer than it should.' : `${count} steps are taking longer than they should.`}
      </p>
      <p className="mt-1 text-sm">The studio keeps checking on them. The details are under Production details.</p>
    </div>
  );
}

// ---------------------------------------------------------------- G1: the shot plan

/**
 * The shot plan: before the run starts (notStarted) and while the run waits for its approval, the
 * owner or an admin picks each shot's place, props and cast and edits its prompts (ShotPlanEditor);
 * the approval waits for unsaved edits. Otherwise the shots are listed. waitingOn (waitsOn): who
 * approves it when the reader may not.
 */
export function ShotPlanSection({ episodeId, state, shots, castHidden, busy, act, canAct, waitingOn = null, notStarted = false,
  planKey, onPlanSaved }: {
  episodeId: string; state: RunState | null; shots: ShotRow[]; castHidden: boolean; busy: boolean; act: Act; canAct: boolean;
  waitingOn?: string | null; notStarted?: boolean; planKey?: string; onPlanSaved?: () => void;
}) {
  const [unsaved, setUnsaved] = useState(false);
  const atG1 = openGate(state) === 'G1_SHOT_PLAN';
  const approved = Boolean(state?.approvals.g1_shot_plan) || Boolean(state?.passed_gates.includes('G1_SHOT_PLAN'));
  const ended = Boolean(state?.run?.ended);
  const editable = canAct && !castHidden && !approved && (notStarted || (atG1 && !ended));
  return (
    <section id="shot-plan" className={`${card} scroll-mt-4`} data-testid="g1-card" aria-labelledby="shot-plan-title">
      <SectionTitle id="shot-plan-title" title="Shot plan" status={gateChip(approved, atG1 && !ended, waitingOn)} />
      {approved && <p className="sr-only" data-testid="g1-approved">Approved</p>}
      <p className="text-gray-700">
        {approved
          ? waitingOn ? 'The shot plan is approved.' : 'You approved the shot plan.'
          : atG1
            ? editable
              ? 'These are the shots the studio plans to make. Pick the place, props and cast of each shot and fix any prompt, then approve them to start making the scene pictures.'
              : 'These are the shots the studio plans to make. Approve them to start making the scene pictures.'
            : notStarted
              ? 'The shots of this video. Pick the place, props and cast of each shot and fix any prompt before it starts.'
              : 'The shot plan is ready to review once your story is approved.'}
      </p>
      {atG1 && !ended && (canAct ? (
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <button data-testid="approve-g1" disabled={busy || unsaved} className={btnPrimary}
            onClick={() => act(() => postJson(`/api/episodes/${episodeId}/approve`, { gate_id: 'g1_shot_plan' }),
              'Shot plan approved.', "Couldn't approve the shot plan")}>
            Approve the shot plan
          </button>
          {unsaved && (
            <span className="text-sm text-amber-800" data-testid="approve-g1-unsaved">
              Save or undo your shot changes first.
            </span>
          )}
        </div>
      ) : <div className="mt-3"><NotYours /></div>)}
      {editable && <ShotPlanEditor episodeId={episodeId} refreshKey={planKey} onDirty={setUnsaved} onSaved={onPlanSaved} />}
      {!editable && shots.length > 0 && (
        <details className="mt-4" open={atG1}>
          <summary className={`cursor-pointer rounded text-sm font-semibold text-gray-800 ${focusRing}`}>
            The {shots.length} shots
          </summary>
          <ol className="mt-2 space-y-2">
            {shots.map((shot) => {
              const text = shot.beat_purpose || (castHidden ? '' : shot.prompt);
              return (
                <li key={shot.shot_id} className="flex gap-3 text-sm">
                  <span className="w-10 shrink-0 font-mono font-semibold">{shot.shot_id}</span>
                  <span className="text-gray-700">{text || 'No description'}</span>
                </li>
              );
            })}
          </ol>
        </details>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- G2: the scene pictures

function StillThumb({ src, alt, testId }: { src: string; alt: string; testId: string }) {
  const [broken, setBroken] = useState(false);
  const frame = 'mx-auto aspect-[9/16] w-full max-w-[160px] rounded-md';
  if (broken) {
    return (
      <div className={`${frame} flex items-center justify-center bg-gray-100 p-2 text-center text-xs text-gray-700`}>
        The picture could not be loaded.
      </div>
    );
  }
  return (
    <a href={src} target="_blank" rel="noopener noreferrer" className={`block rounded-md ${focusRing}`}
      title="Open the full picture in a new tab">
      <img src={src} alt={alt} data-testid={testId} onError={() => setBroken(true)}
        className={`${frame} bg-gray-100 object-cover`} />
    </a>
  );
}

export function SceneStillsSection({ episodeId, state, shotStates, shotStatesError, shots, castHidden, busy, act, canAct,
  waitingOn = null, madeWith = null }: {
  episodeId: string; state: RunState | null; shotStates: Record<string, ShotState> | null; shotStatesError: string | null;
  shots: ShotRow[]; castHidden: boolean; busy: boolean; act: Act; canAct: boolean; waitingOn?: string | null;
  /** Per shot, the characters, place and props its picture was made with (the owner's view only). */
  madeWith?: Record<string, string[]> | null;
}) {
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [pricing, setPricing] = useState<string | null>(null);
  const [redo, setRedo] = useState<{ sid: string; note: string; estimate: RedoEstimate } | null>(null);
  const [redoIssue, setRedoIssue] = useState<{ sid: string; text: string } | null>(null);
  const atG2 = openGate(state) === 'G2_SCENE_STILLS';
  const ended = Boolean(state?.run?.ended);
  const approvedAll = Boolean(state?.approvals.g2_scene_stills) || Boolean(state?.passed_gates.includes('G2_SCENE_STILLS'));
  const ids = runShotIds(state, shotStates);
  const rows = Object.fromEntries(shots.map((s) => [s.shot_id, s]));
  const failed = state ? failedShotIds(state) : [];
  const started = atG2 || approvedAll || ids.length > 0;

  // a redo is paid for: its price (and the note that goes with it) is shown before anything is made
  const priceRedo = async (sid: string, note: string) => {
    setRedo(null);
    setRedoIssue(null);
    setPricing(sid);
    try {
      const estimate = await postJson<RedoEstimate>(`/api/episodes/${episodeId}/shots/${sid}/retry/estimate`,
        redoEstimateRequest(note));
      setRedo({ sid, note, estimate });
    } catch (e) {
      setRedoIssue({ sid, text: problemText(e) });
    } finally {
      setPricing(null);
    }
  };

  const makeRedo = async () => {
    if (!redo) return;
    const { sid, note, estimate } = redo;
    const ok = await act(() => postJson(`/api/episodes/${episodeId}/shots/${sid}/retry`, redoRequest(note, estimate)),
      redoStartedText(sid, note), `Couldn't make picture ${sid} again`);
    setRedo(null);
    if (ok) {
      setReasons((prev) => {
        const next = { ...prev };
        delete next[sid];
        return next;
      });
    }
  };

  return (
    <section id="scene-pictures" className={`${card} scroll-mt-4`} data-testid="shots-review" aria-labelledby="scene-pictures-title">
      <SectionTitle id="scene-pictures-title" title="Scene pictures" status={gateChip(approvedAll, atG2 && !ended, waitingOn)} />
      <p className="text-gray-700">
        {approvedAll
          ? `${waitingOn ? 'The scene pictures are approved.' : 'You approved the scene pictures.'} Each one becomes a short clip.`
          : atG2
            ? 'One picture per shot. Approve the ones that look right, ask for a redo with a note where something should change, then approve them all.'
            : 'The studio makes one picture per shot after you approve the shot plan.'}
      </p>
      {atG2 && !ended && !canAct && <div className="mt-3"><NotYours /></div>}

      {shotStatesError && started && (
        <p role="alert" data-testid="stills-error"
          className="mt-4 rounded-lg bg-red-50 p-3 text-sm text-red-800">
          The scene pictures could not be loaded: {shotStatesError}
        </p>
      )}
      {!shotStatesError && atG2 && ids.length === 0 && (
        <p className="mt-4 text-sm text-gray-700">The pictures are being made. They show up here as they finish.</p>
      )}

      {ids.length > 0 && (
        <ul className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3" data-testid="still-grid">
          {ids.map((sid) => {
            const ss = shotStates?.[sid];
            const row = rows[sid];
            const decision = state?.shot_decisions[sid];
            const retrying = Boolean(state?.pending_retries.includes(sid));
            const status = ss?.still_status ?? state?.shot_outcomes[sid]?.status ?? null;
            const c = stillChip({ stillStatus: status, decision, retrying, held: Boolean(ss?.moderation_escalation), allApproved: approvedAll });
            const description = row?.beat_purpose || (castHidden ? '' : row?.prompt ?? '');
            const note = redoNote(reasons[sid], decision === 'rejected' ? state?.rejection_reasons[sid] : undefined);
            const canDecide = atG2 && !ended && canAct;
            const confirming = redo?.sid === sid ? redo : null;
            return (
              <li key={sid} data-testid={`still-card-${sid}`}
                className="flex flex-col gap-3 rounded-lg border border-gray-200 p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono text-sm font-semibold">{sid}</span>
                  <span className={`${chip} ${tones[c.tone]}`} data-testid={`still-chip-${sid}`}>{c.label}</span>
                </div>
                {row?.still_url ? (
                  <StillThumb src={stillSrc(episodeId, sid, row)} testId={`still-thumb-${sid}`}
                    alt={`Scene picture ${sid}${description ? `: ${description}` : ''}`} />
                ) : (
                  <div className="mx-auto flex aspect-[9/16] w-full max-w-[160px] items-center justify-center rounded-md bg-gray-100 p-2 text-center text-xs text-gray-700">
                    {status === 'failed' ? "This picture couldn't be made." : 'Being made…'}
                  </div>
                )}
                {description && <p className="line-clamp-3 text-sm text-gray-700">{description}</p>}
                {(madeWith?.[sid] ?? []).length > 0 && (
                  <div className="text-xs text-gray-700" data-testid={`made-with-${sid}`}>
                    <span className="mr-1">Made with</span>
                    <ul className="inline-flex flex-wrap gap-1 align-middle">
                      {madeWith?.[sid]?.map((name) => (
                        <li key={name} className={`${chip} ${tones.wait} max-w-full truncate`}>{name}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {!castHidden && ss?.still_error && <p className="text-xs text-red-700">{ss.still_error}</p>}
                {canDecide && (
                  <div className="mt-auto space-y-2">
                    <button data-testid={`approve-shot-${sid}`} disabled={busy || decision === 'approved'} className={`${btnPrimary} w-full`}
                      onClick={() => act(() => apiFetch(`/api/episodes/${episodeId}/shots/${sid}/approve`, { method: 'POST' }),
                        `Picture ${sid} approved.`, `Couldn't approve picture ${sid}`)}>
                      {decision === 'approved' ? 'Approved' : 'Approve'}
                    </button>
                    <label htmlFor={`reject-reason-${sid}`} className="block text-xs font-medium text-gray-700">
                      What should change in this picture?
                    </label>
                    <input id={`reject-reason-${sid}`} data-testid={`reject-reason-${sid}`} value={note} className={input}
                      maxLength={REDO_NOTE_MAX} aria-describedby={`reject-reason-hint-${sid}`}
                      onChange={(e) => {
                        setReasons({ ...reasons, [sid]: e.target.value });
                        if (confirming) setRedo(null);
                      }}
                      placeholder="e.g. the framing is too loose" />
                    <p id={`reject-reason-hint-${sid}`} className="text-xs text-gray-700">
                      Your note goes with the redo when this picture is made again.
                    </p>
                    <button data-testid={`reject-shot-${sid}`} disabled={busy || !note.trim()} className={`${btnSecondary} w-full`}
                      onClick={() => {
                        const form = new FormData();
                        form.append('reason', note.trim());
                        return act(() => apiFetch(`/api/episodes/${episodeId}/shots/${sid}/reject`, { method: 'POST', body: form }),
                          `Redo requested for picture ${sid}.`, `Couldn't ask for a redo of picture ${sid}`);
                      }}>
                      Ask for a redo
                    </button>
                    {(failed.includes(sid) || decision === 'rejected') && (
                      <button data-testid={`retry-shot-${sid}`} disabled={busy || retrying || pricing !== null}
                        className={`${btnSecondary} w-full`} onClick={() => priceRedo(sid, note)}>
                        {pricing === sid ? 'Checking the price…' : 'Make this picture again'}
                      </button>
                    )}
                    {redoIssue?.sid === sid && (
                      <p role="alert" data-testid={`redo-problem-${sid}`} className="text-sm text-red-700">
                        {redoIssue.text}
                      </p>
                    )}
                    {confirming && (
                      <div data-testid={`redo-estimate-${sid}`}
                        className="rounded-lg border border-yellow-200 bg-yellow-50 p-3">
                        <p id={`redo-price-${sid}`} data-testid={`redo-price-${sid}`}
                          className="text-sm font-semibold text-yellow-900">
                          {redoPriceText(confirming.estimate)}
                        </p>
                        <p id={`redo-note-${sid}`} data-testid={`redo-note-${sid}`}
                          className="mt-1 text-xs text-yellow-900">
                          {redoNoteText(confirming.note)}
                        </p>
                        {!confirming.estimate.dry_run && (
                          <p className="mt-1 text-xs text-yellow-900">
                            The studio checks the price, your balance and this video&apos;s budget again before the picture is made.
                          </p>
                        )}
                        <div className="mt-2 flex flex-wrap gap-2">
                          <button type="button" data-testid={`redo-confirm-${sid}`} disabled={busy} className={btnPrimary}
                            aria-describedby={`redo-price-${sid} redo-note-${sid}`} onClick={makeRedo}>
                            Make it again
                          </button>
                          <button type="button" data-testid={`redo-cancel-${sid}`} disabled={busy} className={btnSecondary}
                            onClick={() => setRedo(null)}>
                            Cancel
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {atG2 && !ended && canAct && (
        <div className="mt-6 flex flex-wrap items-center gap-3 border-t border-gray-200 pt-4">
          <button data-testid="approve-g2" disabled={busy} className={btnPrimary}
            onClick={() => act(() => postJson(`/api/episodes/${episodeId}/approve`, { gate_id: 'g2_scene_stills' }),
              'Scene pictures approved. Making the clips now.', "Couldn't approve the scene pictures")}>
            Approve all scene pictures
          </button>
          <span className="text-sm text-gray-700">Approve once every picture has your decision.</span>
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- G_FINAL_CUT: the final cut

function finalBlockers(state: RunState): string[] {
  const out: string[] = [];
  if (state.pending_clip_retries.length) out.push(`a clip is being made again (${state.pending_clip_retries.join(', ')})`);
  if (state.assembly?.blocking) out.push(`the picture check needs attention (${qcLabel(state.assembly.qc?.verdict).toLowerCase()})`);
  if (state.delivery?.due || (state.delivery?.phase && state.delivery.phase !== 'done')) out.push('the final files are still being made');
  if (state.delivery?.blocking) out.push(`the sound and files check needs attention (${qcLabel(state.delivery.qc?.verdict).toLowerCase()})`);
  return out;
}

export function FinalCutSection({ episodeId, state, shotStates, files, busy, act, canAct, waitingOn = null }: {
  episodeId: string; state: RunState | null; shotStates: Record<string, ShotState> | null;
  files: Record<string, FileInfo> | null; busy: boolean; act: Act; canAct: boolean; waitingOn?: string | null;
}) {
  const [finalReason, setFinalReason] = useState('');
  const [clipReasons, setClipReasons] = useState<Record<string, string>>({});
  const atFinal = openGate(state) === 'G_FINAL_CUT';
  const ended = Boolean(state?.run?.ended);
  const completed = ended && state?.run?.outcome === 'completed';
  const approved = Boolean(state?.approvals.g_final_cut);
  const reviewing = Boolean(state) && atFinal && !ended;
  const blockers = state ? finalBlockers(state) : [];
  const downloads = Object.keys(FILE_LABELS).filter((type) => files?.[type]?.available);
  const noFileText = state?.run?.dry_run ? 'A preview run makes no video file.' : 'Not ready yet.';

  return (
    <section id="final-cut" className={`${card} scroll-mt-4`} data-testid="final-cut-card" aria-labelledby="final-cut-title">
      <SectionTitle id="final-cut-title" title="Final cut & delivery" status={gateChip(approved, reviewing, waitingOn)} />
      {approved && (
        <p className="font-semibold text-green-800" data-testid="final-approved">
          {waitingOn ? 'The final cut is approved.' : 'You approved the final cut.'}
        </p>
      )}
      {!approved && !reviewing && (
        <p className="text-gray-700">
          {ended && !completed
            ? 'This run stopped before the final cut.'
            : 'After the clips are made, the studio edits them together with sound. The result shows up here for you to watch and approve.'}
        </p>
      )}

      {reviewing && state && (
        <>
          <p className="text-gray-700">
            Watch the video. Approve it to finish, redo a clip that is not right, or send the cut back with notes.
          </p>
          <div className="mt-4 grid gap-4 md:grid-cols-2">
            {(['phone', 'master_subs'] as const).map((type) => (
              <div key={type}>
                <p className="mb-1 text-sm font-semibold">{fileLabel(type)}</p>
                {files?.[type]?.available ? (
                  <video data-testid={`preview-${type}`} controls playsInline preload="metadata" src={fileUrl(episodeId, type)}
                    className={`w-full rounded bg-black ${type === 'phone' ? 'max-h-[70vh] max-w-xs' : ''}`} />
                ) : (
                  <p className="rounded bg-gray-100 p-3 text-sm text-gray-700" data-testid={`preview-${type}-missing`}>
                    {noFileText}
                  </p>
                )}
              </div>
            ))}
          </div>
          <p className="mt-4 text-sm text-gray-700">
            Picture check: {qcLabel(state.assembly?.qc?.verdict)} · Sound and files check: {qcLabel(state.delivery?.qc?.verdict)}
          </p>
          {blockers.length > 0 && (
            <p className="mt-2 text-sm text-red-800" data-testid="final-blockers">
              Approving will be refused until {blockers.join('; ')}.
            </p>
          )}
          {state.final_cut_refusals.length > 0 && (
            <p className="mt-2 text-xs text-gray-700">
              Last time approval was refused: {refusalText(state.final_cut_refusals[state.final_cut_refusals.length - 1])}
            </p>
          )}
          {state.final_cut_rejections.length > 0 && (
            <p className="mt-2 text-sm text-gray-700" data-testid="final-rejections">
              Your notes so far: {state.final_cut_rejections.map((r) => r.reason).join(' | ')}
            </p>
          )}

          {canAct ? (
            <>
              <details className="mt-4" open>
                <summary className={`cursor-pointer rounded text-sm font-semibold ${focusRing}`}>Redo a clip</summary>
                <ul className="mt-2 space-y-2">
                  {runShotIds(state, shotStates).map((sid) => {
                    const reason = clipReasons[sid] ?? '';
                    return (
                      <li key={sid} className="flex flex-wrap items-center gap-2">
                        <span className="w-10 font-mono text-sm font-semibold">{sid}</span>
                        <input data-testid={`clip-reason-${sid}`} value={reason} aria-label={`What should change in clip ${sid}?`}
                          onChange={(e) => setClipReasons({ ...clipReasons, [sid]: e.target.value })}
                          placeholder="What should change?" className={`${input} max-w-xs flex-1`} />
                        <button data-testid={`retry-clip-${sid}`} disabled={busy || !reason.trim()} className={btnSecondary}
                          onClick={() => act(() => postJson(`/api/episodes/${episodeId}/shots/${sid}/retry-clip`, { reason: reason.trim() }),
                            `Making the clip for ${sid} again.`, `Couldn't redo the clip for ${sid}`)}>
                          Redo this clip
                        </button>
                        {state.pending_clip_retries.includes(sid) && <span className={`${chip} ${tones.wait}`}>Being made again</span>}
                      </li>
                    );
                  })}
                </ul>
              </details>
              <div className="mt-6 flex flex-col gap-3 border-t border-gray-200 pt-4">
                <div>
                  {/* Deliberately NOT disabled by the blocker list: the API decides and its 409 is shown. */}
                  <button data-testid="approve-final" disabled={busy} className={btnPrimary}
                    onClick={() => act(() => postJson(`/api/episodes/${episodeId}/approve`, { gate_id: 'g_final_cut' }),
                      'Final cut approved.', "Couldn't approve the final cut")}>
                    Approve the final cut
                  </button>
                </div>
                <label htmlFor="final-reject-reason" className="text-sm font-medium text-gray-700">
                  Or send it back: what should change?
                </label>
                <textarea id="final-reject-reason" data-testid="final-reject-reason" value={finalReason} rows={2}
                  onChange={(e) => setFinalReason(e.target.value)} className={`${input} max-w-xl`}
                  placeholder="e.g. the pacing needs another pass" />
                <div>
                  <button data-testid="reject-final" disabled={busy || !finalReason.trim()} className={btnSecondary}
                    onClick={async () => {
                      const ok = await act(() => postJson(`/api/episodes/${episodeId}/final-cut/reject`, { reason: finalReason.trim() }),
                        'Final cut sent back with your notes.', "Couldn't send the final cut back");
                      if (ok) setFinalReason('');
                    }}>
                    Send it back
                  </button>
                </div>
              </div>
            </>
          ) : <div className="mt-4"><NotYours /></div>}
        </>
      )}

      {!completed && downloads.length > 0 && (
        <div className="mt-4" data-testid="downloads">
          <h3 className="text-sm font-semibold">Download</h3>
          <ul className="mt-1 space-y-1 text-sm">
            {downloads.map((type) => (
              <li key={type}>
                <a data-testid={`download-${type}`} className="text-blue-700 underline-offset-2 hover:underline"
                  href={`${fileUrl(episodeId, type)}?download=true`}>
                  {fileLabel(type)}
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- Production details

const sub = 'rounded-lg border border-gray-200 p-4';

function QcBlock({ title, testid, view, onAccept, busy, canAccept }: {
  title: string; testid: string; view: StageView | null; busy: boolean; canAccept: boolean;
  onAccept: (reason: string) => Promise<boolean>;
}) {
  const [reason, setReason] = useState('');
  if (!view) {
    return (
      <div className={sub} data-testid={testid}>
        <h3 className="font-semibold">{title}</h3>
        <p className="text-sm text-gray-700">Not produced yet.</p>
      </div>
    );
  }
  const verdict = view.qc?.verdict ?? 'unknown';
  return (
    <div className={sub} data-testid={testid}>
      <h3 className="mb-1 font-semibold">{title}</h3>
      <p className="text-sm">
        Verdict: <strong data-testid={`${testid}-verdict`}>{verdict}</strong> ({qcLabel(verdict)})
        {' · '}
        <span data-testid={`${testid}-blocking`} className={view.blocking ? 'font-semibold text-red-700' : 'text-green-800'}>
          {view.blocking ? 'blocking' : 'not blocking'}
        </span>
        {view.stale && <span className="ml-2 text-amber-800">(stale: a clip changed since this was built)</span>}
      </p>
      {view.qc?.reason && <p className="mt-1 text-xs text-gray-700">{view.qc.reason}</p>}
      {view.acceptance?.reason && (
        <p className="mt-1 text-xs text-blue-800" data-testid={`${testid}-accepted`}>Accepted: {view.acceptance.reason}</p>
      )}
      {view.blocking && canAccept && (
        <div className="mt-3 flex flex-wrap gap-2">
          <input data-testid={`${testid}-accept-reason`} value={reason} onChange={(e) => setReason(e.target.value)}
            aria-label={`Why accept this ${title.toLowerCase()} verdict`} placeholder="Reason for accepting this verdict"
            className={`${input} max-w-md flex-1`} />
          <button data-testid={`${testid}-accept`} disabled={busy || !reason.trim()} className={btnDanger}
            onClick={async () => { if (await onAccept(reason.trim())) setReason(''); }}>
            Accept with reason
          </button>
        </div>
      )}
    </div>
  );
}

function CreditsCard({ episodeId, credits, busy, act, isAdmin }: {
  episodeId: string; credits: Credits | null; busy: boolean; act: Act; isAdmin: boolean;
}) {
  const [newBalance, setNewBalance] = useState('');
  const [newCap, setNewCap] = useState('');
  const bd = balanceDisplay(credits?.higgsfield_balance);
  const tone = bd.tone === 'ok' ? 'text-green-800' : bd.tone === 'warn' ? 'text-amber-800' : 'text-red-700';
  return (
    <div className={sub} data-testid="credits-card">
      <h3 className="mb-1 font-semibold">Credit cap and balance</h3>
      {credits ? (
        <p className="text-sm">
          Spent <strong>{credits.spent_credits}</strong> · reserved {credits.reserved_credits} · cap{' '}
          <strong data-testid="credit-cap">{credits.cap_credits || 'not set'}</strong>
          {credits.pause_needed && <span className="ml-2 font-semibold text-red-700" data-testid="cap-paused">at cap: shots paused</span>}
        </p>
      ) : <p className="text-sm text-gray-700">No credit line yet.</p>}
      <p className={`mt-1 text-sm ${tone}`} data-testid="higgsfield-balance" data-tone={bd.tone}>{bd.text}</p>
      {isAdmin && (
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <div>
            <label htmlFor="new-balance" className="block text-xs font-medium text-gray-700">Provider balance (USD)</label>
            <div className="mt-1 flex gap-2">
              <input id="new-balance" data-testid="new-balance" value={newBalance} inputMode="decimal" className={input}
                onChange={(e) => setNewBalance(e.target.value.replace(/[^0-9.]/g, ''))} placeholder="e.g. 42.50" />
              <button data-testid="record-balance" disabled={busy || parseBalanceInput(newBalance) === null} className={btnSecondary}
                onClick={async () => {
                  const ok = await act(() => postJson('/api/budget/balance', { balance_usd: parseBalanceInput(newBalance), note: 'studio credits panel' }),
                    'Balance recorded.', "Couldn't record the balance");
                  if (ok) setNewBalance('');
                }}>
                Record
              </button>
            </div>
          </div>
          <div>
            <label htmlFor="new-cap" className="block text-xs font-medium text-gray-700">New cap (credits)</label>
            <div className="mt-1 flex gap-2">
              <input id="new-cap" data-testid="new-cap" value={newCap} inputMode="numeric" className={input}
                onChange={(e) => setNewCap(e.target.value.replace(/[^0-9]/g, ''))} placeholder="e.g. 650" />
              <button data-testid="raise-cap" disabled={busy || !newCap} className={btnSecondary}
                onClick={async () => {
                  const ok = await act(() => postJson(`/api/episodes/${episodeId}/raise-cap`, { new_cap: Number(newCap) }),
                    'Credit cap raised.', "Couldn't raise the cap");
                  if (ok) setNewCap('');
                }}>
                Raise cap
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

const ENDED_LABELS: Record<string, string> = { completed: 'Finished', expired: 'History expired' };

/** Raw run state, credits, moderation, QC and per-shot workflow state, for Production details. */
export function RunDetails({ episodeId, state, shotStates, credits, stalls, busy, act, isAdmin }: {
  episodeId: string; state: RunState; shotStates: Record<string, ShotState> | null; credits: Credits | null;
  stalls: StallAlert[]; busy: boolean; act: Act; isAdmin: boolean;
}) {
  const ended = Boolean(state.run?.ended);
  const ids = runShotIds(state, shotStates);
  const moderated = ids.filter((sid) => {
    const m = shotStates?.[sid]?.moderation;
    return (m && Object.values(m).some((p) => p.steps.length > 0)) || !!shotStates?.[sid]?.moderation_escalation;
  });
  const failed = failedShotIds(state);
  return (
    <>
      <div className={sub} data-testid="run-status">
        <h3 className="mb-1 font-semibold">Run</h3>
        <p className="text-sm">Stage: <code data-testid="run-stage">{state.stage ?? '—'}</code> ({stageLabel(state.stage)})</p>
        {ended ? (
          <p className="text-sm">
            Gate: <strong data-testid="run-finished">{ENDED_LABELS[state.run?.outcome ?? ''] ?? 'Stopped'}</strong> (run {state.run?.outcome})
          </p>
        ) : (
          <p className="text-sm">
            Gate: <code data-testid="current-gate">{state.current_gate ?? 'none'}</code>
            {state.current_gate && <> ({gateLabel(state.current_gate)}, {openGate(state) ? 'waiting for a decision' : 'passed'})</>}
          </p>
        )}
        <p className="text-sm">Passed gates: <span data-testid="passed-gates">{state.passed_gates.join(', ') || '—'}</span></p>
        {failed.length > 0 && <p className="text-sm text-red-700">Failed shots: {failed.join(', ')}</p>}
        {state.run && (
          <p className="text-xs text-gray-700">
            Run {state.run.run_id ?? 'unknown'} · {state.run.status}{state.run.dry_run ? ' · dry run' : ''}
          </p>
        )}
        {state.run?.archived && (
          <p className="mt-1 text-xs text-gray-700" data-testid="run-archived">
            The workflow server no longer keeps this run&apos;s history.{' '}
            {state.run.outcome !== 'completed'
              ? 'Nothing saved shows how it ended.'
              : state.run.saved_at
                ? `This is the run as saved on ${formatLocalDateTime(state.run.saved_at) || state.run.saved_at}.`
                : 'This is the run as rebuilt from its saved files.'}
          </p>
        )}
        {stalls.length > 0 && (
          <ul className="mt-2 text-sm" data-testid="stall-list">
            {stalls.map((a) => (
              <li key={a.job_id}>Stalled: {a.shot_id ?? '—'} · {a.job_type} · {a.minutes_stalled} min · {a.status}</li>
            ))}
          </ul>
        )}
      </div>

      <CreditsCard episodeId={episodeId} credits={credits} busy={busy} act={act} isAdmin={isAdmin} />

      <div className={sub} data-testid="moderation-card">
        <h3 className="mb-1 font-semibold">Moderation ladder</h3>
        {moderated.length === 0 ? (
          <p className="text-sm text-gray-700" data-testid="moderation-none">No moderation steps were needed.</p>
        ) : (
          <ul className="space-y-1 text-sm">
            {moderated.map((sid) => (
              <li key={sid} data-testid={`moderation-${sid}`}>
                <strong>{sid}</strong>{' '}
                {Object.entries(shotStates?.[sid]?.moderation ?? {}).map(([phase, m]) => (
                  m.steps.length ? <span key={phase} className="mr-3">{phase}: {m.steps.join(', then ')}</span> : null
                ))}
                {shotStates?.[sid]?.moderation_escalation
                  ? <span className="text-red-700">escalated: {refusalText(shotStates?.[sid]?.moderation_escalation)}</span>
                  : null}
              </li>
            ))}
          </ul>
        )}
      </div>

      <QcBlock title="Picture assembly QC" testid="assembly-qc" view={state.assembly} busy={busy} canAccept={isAdmin && !ended}
        onAccept={(reason) => act(() => postJson(`/api/episodes/${episodeId}/assembly/accept`, { reason }),
          'Picture check accepted.', "Couldn't accept the picture check")} />
      <QcBlock title="Delivery QC" testid="delivery-qc" view={state.delivery} busy={busy} canAccept={isAdmin && !ended}
        onAccept={(reason) => act(() => postJson(`/api/episodes/${episodeId}/delivery/accept`, { reason }),
          'Delivery check accepted.', "Couldn't accept the delivery check")} />
    </>
  );
}

/** The read-only shot list for Production details: the workflow's view of each shot and the studio database's. */
export function ShotListDetails({ episodeId, state, shotStates, shots, castHidden, preflight }: {
  episodeId: string; state: RunState | null; shotStates: Record<string, ShotState> | null; shots: ShotRow[];
  castHidden: boolean; preflight: Record<string, ShotPreflight> | null;
}) {
  const rows = Object.fromEntries(shots.map((s) => [s.shot_id, s]));
  const ids = Array.from(new Set([...runShotIds(state, shotStates), ...shots.map((s) => s.shot_id)])).sort();
  return (
    <div className={sub} data-testid="shots-card">
      <h3 className="mb-2 font-semibold">Shot list ({ids.length} shots)</h3>
      {ids.length === 0 && <p className="text-sm text-gray-700">No shots loaded. Upload BEATMAP.md to load shots.</p>}
      <ul className="space-y-3">
        {ids.map((sid) => {
          const ss = shotStates?.[sid];
          const row = rows[sid];
          const decision = state?.shot_decisions[sid];
          const check = preflight?.[sid];
          return (
            <li key={sid} className="rounded border border-gray-200 p-3" data-testid={`shot-${sid}`}>
              <div className="flex flex-wrap items-center gap-3 text-sm">
                <strong>{sid}</strong>
                {row?.beat_id && <span className={`${chip} ${tones.info}`} title={row.beat_purpose}>{row.beat_id}</span>}
                {check && (
                  <span className={check.ok ? 'text-green-800' : 'text-red-700'}
                    title={check.errors.join('\n') || 'Passes model limits'}>
                    {check.ok ? 'within limits' : 'breaks a limit'}
                  </span>
                )}
                {ss?.pre_approved && <span data-testid={`shot-${sid}-pre-approved`} className={`${chip} ${tones.todo}`}>pre-approved still</span>}
                <span>still: <span data-testid={`shot-${sid}-still`}>{ss?.still_status ?? state?.shot_outcomes[sid]?.status ?? '—'}</span></span>
                <span>clip: <span data-testid={`shot-${sid}-clip`}>{ss?.clip_status ?? '—'}</span></span>
                {decision && <span data-testid={`shot-${sid}-decision`} className="font-semibold">{decision}</span>}
                {state?.clip_retries[sid] ? <span>clip retries: {state.clip_retries[sid]}</span> : null}
                {state?.pending_clip_retries.includes(sid) && <span className="text-amber-800">clip retry running</span>}
                {row && <span className="text-gray-700">db: {row.status}{row.retries > 0 ? ` (retry ${row.retries})` : ''}</span>}
              </div>
              {(ss?.still_error || ss?.clip_error) && <p className="mt-1 text-xs text-red-700">{ss?.still_error ?? ss?.clip_error}</p>}
              {row?.beat_purpose && <p className="mt-1 text-sm italic text-purple-800">{row.beat_purpose}</p>}
              {row?.prompt && !castHidden && <p className="mt-1 text-sm text-gray-700">{row.prompt}</p>}
              {row?.still_url && (
                <div className="mt-2 text-xs">
                  Still
                  {row.pre_approved && (
                    <span data-testid={`still-${sid}-pre-approved`} className={`ml-2 ${chip} ${tones.todo}`}>pre-approved (no still spend)</span>
                  )}
                  <img src={stillSrc(episodeId, sid, row)} alt={`Still ${sid}`} loading="lazy"
                    className="mt-1 h-32 w-auto rounded border border-gray-300 object-cover" />
                </div>
              )}
              {row?.clip_url && (
                <video src={row.clip_url} controls preload="none" className="mt-2 w-64 rounded border border-gray-300" />
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
