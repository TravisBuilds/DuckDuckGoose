'use client';

import { useCallback, useEffect, useState } from 'react';
import { API_URL, ApiError, apiFetch, postJson } from '@/lib/studio-api';

/**
 * Drives a whole episode run from the studio (G-UI-1): G1 approval, per-shot G2 decisions,
 * clip retries, credit-cap raise, stall + moderation state, assembly / delivery QC with
 * accept-with-reason, the G_FINAL_CUT preview and approve / reject, and downloads.
 * Every action is an existing API route; the API (and behind it the workflow) stays the
 * authority, so a refused action shows the API's reason instead of pretending it worked.
 */

interface QcView { verdict?: string; blocking?: boolean; reason?: string; checks?: unknown }
interface StageView {
  qc?: QcView;
  acceptance?: { reason?: string } | null;
  blocking?: boolean;
  stale?: boolean;
  edit_version?: number;
  due?: boolean;
}
export interface RunState {
  episode_id: string;
  stage: string | null;
  current_gate: string | null;
  passed_gates: string[];
  approvals: Record<string, boolean>;
  shot_decisions: Record<string, string>;
  pending_retries: string[];
  clip_retries: Record<string, number>;
  pending_clip_retries: string[];
  final_cut_refusals: { reason?: string }[] | string[];
  final_cut_rejections: { reason?: string }[];
  failed_shots: string[];
  shot_outcomes: Record<string, { status?: string; still_url?: string | null; clip_url?: string | null }>;
  active_stall_alerts: number;
  assembly: StageView | null;
  delivery: StageView | null;
}
interface ShotState {
  available: boolean;
  still_status?: string;
  clip_status?: string;
  still_error?: string | null;
  clip_error?: string | null;
  moderation?: Record<string, { steps: string[]; retry: unknown }>;
  moderation_escalation?: unknown;
  reason?: string;
}
interface Credits {
  cap_credits: number; spent_credits: number; reserved_credits: number; available_credits: number;
  pause_threshold: number; pause_needed: boolean; over_cap: boolean;
}
interface StallAlert { job_id: string; shot_id: string | null; job_type: string; minutes_stalled: number; status: string }
interface FileInfo { available: boolean; size_bytes?: number; url?: string; reason?: string }

/** Older API builds omit the run-control fields: default them instead of crashing the page. */
function normalizeRunState(episodeId: string, raw: Partial<RunState>): RunState {
  return {
    episode_id: raw.episode_id ?? episodeId,
    stage: raw.stage ?? null,
    current_gate: raw.current_gate ?? null,
    passed_gates: raw.passed_gates ?? [],
    approvals: raw.approvals ?? {},
    shot_decisions: raw.shot_decisions ?? {},
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
  };
}

const card = 'bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 mb-6';
const btn = 'px-3 py-1.5 rounded text-sm font-semibold disabled:opacity-50';
const FILE_LABELS: Record<string, string> = {
  master_subs: 'Delivery master (subtitled)',
  master_nosubs: 'Delivery master (no subs)',
  phone: 'Phone version',
  subtitles_srt: 'Subtitles (.srt)',
  mute_edit: 'Mute picture edit',
};

function refusalText(r: unknown): string {
  if (typeof r === 'string') return r;
  if (r && typeof r === 'object' && 'reason' in r) return String((r as { reason: unknown }).reason);
  return JSON.stringify(r);
}

function QcBlock({ title, testid, view, onAccept, busy }: {
  title: string; testid: string; view: StageView | null; busy: boolean;
  onAccept: (reason: string) => Promise<void>;
}) {
  const [reason, setReason] = useState('');
  if (!view) {
    return <div className={card} data-testid={testid}><h2 className="text-lg font-semibold">{title}</h2><p className="text-sm text-gray-500">Not produced yet.</p></div>;
  }
  const verdict = view.qc?.verdict ?? 'unknown';
  return (
    <div className={card} data-testid={testid}>
      <h2 className="text-lg font-semibold mb-2">{title}</h2>
      <p className="text-sm">
        Verdict: <strong data-testid={`${testid}-verdict`}>{verdict}</strong>
        {' · '}
        <span data-testid={`${testid}-blocking`} className={view.blocking ? 'text-red-700 font-semibold' : 'text-green-700'}>
          {view.blocking ? 'blocking' : 'not blocking'}
        </span>
        {view.stale && <span className="ml-2 text-yellow-700">(stale: a clip changed since this was built)</span>}
      </p>
      {view.qc?.reason && <p className="text-xs text-gray-600 mt-1">{view.qc.reason}</p>}
      {view.acceptance?.reason && (
        <p className="text-xs text-blue-700 mt-1" data-testid={`${testid}-accepted`}>Accepted: {view.acceptance.reason}</p>
      )}
      {view.blocking && (
        <div className="mt-3 flex gap-2">
          <input
            data-testid={`${testid}-accept-reason`}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Reason for accepting this verdict"
            className="flex-1 px-2 py-1 border rounded text-sm bg-white dark:bg-gray-900"
          />
          <button
            data-testid={`${testid}-accept`}
            disabled={busy || !reason.trim()}
            onClick={async () => { await onAccept(reason.trim()); setReason(''); }}
            className={`${btn} bg-yellow-600 text-white`}
          >
            Accept with reason
          </button>
        </div>
      )}
    </div>
  );
}

export default function EpisodeRunPanel({ episodeId, onChanged }: { episodeId: string; onChanged?: () => void }) {
  const [state, setState] = useState<RunState | null>(null);
  const [shotStates, setShotStates] = useState<Record<string, ShotState>>({});
  const [credits, setCredits] = useState<Credits | null>(null);
  const [stalls, setStalls] = useState<StallAlert[]>([]);
  const [files, setFiles] = useState<Record<string, FileInfo>>({});
  const [loadError, setLoadError] = useState<string | null>(null);
  const [message, setMessage] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [rejectReasons, setRejectReasons] = useState<Record<string, string>>({});
  const [clipReasons, setClipReasons] = useState<Record<string, string>>({});
  const [newCap, setNewCap] = useState('');
  const [finalRejectReason, setFinalRejectReason] = useState('');

  const refresh = useCallback(async () => {
    try {
      const raw = await apiFetch<Partial<RunState>>(`/api/episodes/${episodeId}`);
      setState(normalizeRunState(episodeId, raw));
      setLoadError(null);
      const [ss, cr, st, fl] = await Promise.allSettled([
        apiFetch<{ shots: Record<string, ShotState> }>(`/api/episodes/${episodeId}/shot-states`),
        apiFetch<Credits>(`/api/episodes/${episodeId}/credits`),
        apiFetch<{ alerts: StallAlert[] }>(`/api/episodes/${episodeId}/stalls`),
        apiFetch<{ files: Record<string, FileInfo> }>(`/api/episodes/${episodeId}/files`),
      ]);
      if (ss.status === 'fulfilled' && ss.value?.shots) setShotStates(ss.value.shots);
      if (cr.status === 'fulfilled' && cr.value && typeof cr.value.cap_credits === 'number') setCredits(cr.value);
      if (st.status === 'fulfilled' && Array.isArray(st.value?.alerts)) setStalls(st.value.alerts.filter((a) => a.status !== 'resolved'));
      if (fl.status === 'fulfilled' && fl.value?.files) setFiles(fl.value.files);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    }
  }, [episodeId]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 3000);
    return () => clearInterval(t);
  }, [refresh]);

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(true);
    setMessage(null);
    try {
      await fn();
      setMessage({ kind: 'ok', text: `${label}: done` });
    } catch (e) {
      const text = e instanceof ApiError ? `${label} refused (${e.status}): ${e.message}` : `${label} failed: ${String(e)}`;
      setMessage({ kind: 'error', text });
    } finally {
      setBusy(false);
      await refresh();
      onChanged?.();
    }
  };

  if (loadError && !state) {
    return <div className={card} data-testid="run-panel-error"><p className="text-sm text-red-700">Run state unavailable: {loadError}</p></div>;
  }
  if (!state) return <div className={card}>Loading run state…</div>;

  const gate = state.current_gate;
  const shotIds = Array.from(new Set([
    ...Object.keys(shotStates), ...Object.keys(state.shot_outcomes), ...Object.keys(state.shot_decisions),
  ])).sort();
  const moderated = shotIds.filter((sid) => {
    const m = shotStates[sid]?.moderation;
    return (m && Object.values(m).some((p) => p.steps.length > 0)) || !!shotStates[sid]?.moderation_escalation;
  });
  const finalBlockers: string[] = [];
  if (state.pending_clip_retries.length) finalBlockers.push(`clip retries pending: ${state.pending_clip_retries.join(', ')}`);
  if (state.assembly?.blocking) finalBlockers.push(`assembly QC blocking (${state.assembly.qc?.verdict})`);
  if (state.delivery?.blocking) finalBlockers.push(`delivery QC blocking (${state.delivery.qc?.verdict})`);
  const previewUrl = (type: string) => `${API_URL}/api/episodes/${episodeId}/files/${type}`;

  return (
    <div data-testid="run-panel">
      {message && (
        <div role="status" data-testid="run-message"
          className={`mb-4 p-3 rounded text-sm ${message.kind === 'ok' ? 'bg-green-50 text-green-800' : 'bg-red-50 text-red-800'}`}>
          {message.text}
        </div>
      )}

      {(stalls.length > 0 || state.active_stall_alerts > 0) && (
        <div className="mb-6 p-4 rounded-lg border border-orange-300 bg-orange-50 text-orange-900" data-testid="stall-banner">
          <p className="font-semibold">⚠ {Math.max(stalls.length, state.active_stall_alerts)} stalled job(s)</p>
          <ul className="text-sm mt-1">
            {stalls.map((a) => (
              <li key={a.job_id}>{a.shot_id ?? '—'} · {a.job_type} · {a.minutes_stalled} min · {a.status}</li>
            ))}
          </ul>
        </div>
      )}

      <div className={card} data-testid="run-status">
        <h2 className="text-lg font-semibold mb-2">Run</h2>
        <p className="text-sm">Stage: <strong data-testid="run-stage">{state.stage ?? '—'}</strong></p>
        <p className="text-sm">Waiting at gate: <strong data-testid="current-gate">{gate ?? 'none'}</strong></p>
        <p className="text-sm">Passed gates: <span data-testid="passed-gates">{state.passed_gates.join(', ') || '—'}</span></p>
        {state.failed_shots.length > 0 && <p className="text-sm text-red-700">Failed shots: {state.failed_shots.join(', ')}</p>}
        {state.approvals.g_final_cut && <p className="text-sm text-green-700 font-semibold" data-testid="final-approved">Final cut approved</p>}
      </div>

      <div className={card} data-testid="credits-card">
        <h2 className="text-lg font-semibold mb-2">Credit cap</h2>
        {credits ? (
          <p className="text-sm">
            Spent <strong>{credits.spent_credits}</strong> · reserved {credits.reserved_credits} · cap <strong data-testid="credit-cap">{credits.cap_credits || 'not set'}</strong>
            {credits.pause_needed && <span className="ml-2 text-red-700 font-semibold" data-testid="cap-paused">at cap: shots paused</span>}
          </p>
        ) : <p className="text-sm text-gray-500">No credit line yet.</p>}
        <div className="mt-2 flex gap-2">
          <input data-testid="new-cap" value={newCap} onChange={(e) => setNewCap(e.target.value.replace(/[^0-9]/g, ''))}
            placeholder="New cap (credits)" className="w-48 px-2 py-1 border rounded text-sm bg-white dark:bg-gray-900" />
          <button data-testid="raise-cap" disabled={busy || !newCap}
            onClick={() => act('Raise cap', () => postJson(`/api/episodes/${episodeId}/raise-cap`, { new_cap: Number(newCap) }).then(() => setNewCap('')))}
            className={`${btn} bg-blue-600 text-white`}>Raise cap</button>
        </div>
      </div>

      <div className={card} data-testid="g1-card">
        <h2 className="text-lg font-semibold mb-2">G1: shot plan</h2>
        {state.approvals.g1_shot_plan ? <p className="text-sm text-green-700" data-testid="g1-approved">Approved</p> : (
          <button data-testid="approve-g1" disabled={busy || gate !== 'G1_SHOT_PLAN'}
            onClick={() => act('Approve G1', () => postJson(`/api/episodes/${episodeId}/approve`, { gate_id: 'g1_shot_plan' }))}
            className={`${btn} bg-green-600 text-white`}>Approve shot plan</button>
        )}
      </div>

      <div className={card} data-testid="shots-card">
        <h2 className="text-lg font-semibold mb-2">Shots (G2 decisions, retries, moderation)</h2>
        {shotIds.length === 0 && <p className="text-sm text-gray-500">No shots running yet.</p>}
        <div className="space-y-3">
          {shotIds.map((sid) => {
            const ss = shotStates[sid];
            const decision = state.shot_decisions[sid];
            const atG2 = gate === 'G2_SCENE_STILLS';
            return (
              <div key={sid} className="border rounded p-3" data-testid={`shot-${sid}`}>
                <div className="flex flex-wrap items-center gap-3 text-sm">
                  <strong>{sid}</strong>
                  <span>still: <span data-testid={`shot-${sid}-still`}>{ss?.still_status ?? state.shot_outcomes[sid]?.status ?? '—'}</span></span>
                  <span>clip: <span data-testid={`shot-${sid}-clip`}>{ss?.clip_status ?? '—'}</span></span>
                  {decision && <span data-testid={`shot-${sid}-decision`} className="font-semibold">{decision}</span>}
                  {state.clip_retries[sid] ? <span>clip retries: {state.clip_retries[sid]}</span> : null}
                  {state.pending_clip_retries.includes(sid) && <span className="text-yellow-700">clip retry running</span>}
                </div>
                {(ss?.still_error || ss?.clip_error) && <p className="text-xs text-red-700 mt-1">{ss?.still_error ?? ss?.clip_error}</p>}
                {atG2 && (
                  <div className="mt-2 flex flex-wrap gap-2 items-center">
                    <button data-testid={`approve-shot-${sid}`} disabled={busy}
                      onClick={() => act(`Approve ${sid}`, () => apiFetch(`/api/episodes/${episodeId}/shots/${sid}/approve`, { method: 'POST' }))}
                      className={`${btn} bg-green-600 text-white`}>Approve still</button>
                    <input data-testid={`reject-reason-${sid}`} value={rejectReasons[sid] ?? ''}
                      onChange={(e) => setRejectReasons({ ...rejectReasons, [sid]: e.target.value })}
                      placeholder="Rejection reason" className="px-2 py-1 border rounded text-sm bg-white dark:bg-gray-900" />
                    <button data-testid={`reject-shot-${sid}`} disabled={busy || !(rejectReasons[sid] ?? '').trim()}
                      onClick={() => act(`Reject ${sid}`, () => {
                        const form = new FormData();
                        form.append('reason', (rejectReasons[sid] ?? '').trim());
                        return apiFetch(`/api/episodes/${episodeId}/shots/${sid}/reject`, { method: 'POST', body: form });
                      })}
                      className={`${btn} bg-red-600 text-white`}>Reject</button>
                    {(state.failed_shots.includes(sid) || decision === 'rejected') && (
                      <button data-testid={`retry-shot-${sid}`} disabled={busy}
                        onClick={() => act(`Retry ${sid}`, () => apiFetch(`/api/episodes/${episodeId}/shots/${sid}/retry`, { method: 'POST' }))}
                        className={`${btn} bg-gray-700 text-white`}>Retry still</button>
                    )}
                  </div>
                )}
                {gate === 'G_FINAL_CUT' && (
                  <div className="mt-2 flex flex-wrap gap-2 items-center">
                    <input data-testid={`clip-reason-${sid}`} value={clipReasons[sid] ?? ''}
                      onChange={(e) => setClipReasons({ ...clipReasons, [sid]: e.target.value })}
                      placeholder="Why re-run this clip" className="px-2 py-1 border rounded text-sm bg-white dark:bg-gray-900" />
                    <button data-testid={`retry-clip-${sid}`} disabled={busy || !(clipReasons[sid] ?? '').trim()}
                      onClick={() => act(`Retry clip ${sid}`, () => postJson(`/api/episodes/${episodeId}/shots/${sid}/retry-clip`, { reason: (clipReasons[sid] ?? '').trim() }))}
                      className={`${btn} bg-gray-700 text-white`}>Retry clip</button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
        {gate === 'G2_SCENE_STILLS' && (
          <button data-testid="approve-g2" disabled={busy}
            onClick={() => act('Approve G2', () => postJson(`/api/episodes/${episodeId}/approve`, { gate_id: 'g2_scene_stills' }))}
            className={`${btn} bg-green-700 text-white mt-4`}>Approve G2 (all stills)</button>
        )}
      </div>

      <div className={card} data-testid="moderation-card">
        <h2 className="text-lg font-semibold mb-2">Moderation ladder</h2>
        {moderated.length === 0 ? <p className="text-sm text-gray-500" data-testid="moderation-none">No moderation rungs taken.</p> : (
          <ul className="text-sm space-y-1">
            {moderated.map((sid) => (
              <li key={sid} data-testid={`moderation-${sid}`}>
                <strong>{sid}</strong>{' '}
                {Object.entries(shotStates[sid]?.moderation ?? {}).map(([phase, m]) => (
                  m.steps.length ? <span key={phase} className="mr-3">{phase}: {m.steps.join(' → ')}</span> : null
                ))}
                {shotStates[sid]?.moderation_escalation ? <span className="text-red-700">escalated: {refusalText(shotStates[sid]?.moderation_escalation)}</span> : null}
              </li>
            ))}
          </ul>
        )}
      </div>

      <QcBlock title="Picture assembly QC" testid="assembly-qc" view={state.assembly} busy={busy}
        onAccept={(reason) => act('Accept assembly QC', () => postJson(`/api/episodes/${episodeId}/assembly/accept`, { reason }))} />
      <QcBlock title="Delivery QC" testid="delivery-qc" view={state.delivery} busy={busy}
        onAccept={(reason) => act('Accept delivery QC', () => postJson(`/api/episodes/${episodeId}/delivery/accept`, { reason }))} />

      <div className={card} data-testid="final-cut-card">
        <h2 className="text-lg font-semibold mb-2">G_FINAL_CUT: final review</h2>
        <div className="grid md:grid-cols-2 gap-4 mb-4">
          {(['master_subs', 'phone'] as const).map((type) => (
            <div key={type}>
              <p className="text-sm font-semibold mb-1">{FILE_LABELS[type]}</p>
              {files[type]?.available ? (
                <video data-testid={`preview-${type}`} controls preload="metadata" src={previewUrl(type)} className="w-full bg-black rounded" />
              ) : <p className="text-xs text-gray-500" data-testid={`preview-${type}-missing`}>Not rendered yet.</p>}
            </div>
          ))}
        </div>
        <h3 className="text-sm font-semibold">Downloads</h3>
        <ul className="text-sm mb-4" data-testid="downloads">
          {Object.entries(FILE_LABELS).map(([type, label]) => (
            <li key={type}>
              {files[type]?.available
                ? <a data-testid={`download-${type}`} className="text-blue-600 hover:underline" href={`${previewUrl(type)}?download=true`}>{label}</a>
                : <span className="text-gray-400">{label} (not available)</span>}
            </li>
          ))}
        </ul>
        {finalBlockers.length > 0 && gate === 'G_FINAL_CUT' && (
          <p className="text-sm text-red-700 mb-2" data-testid="final-blockers">Approval will be refused: {finalBlockers.join('; ')}</p>
        )}
        {state.final_cut_refusals.length > 0 && (
          <p className="text-xs text-gray-600 mb-2">Last workflow refusal: {refusalText(state.final_cut_refusals[state.final_cut_refusals.length - 1])}</p>
        )}
        {state.final_cut_rejections.length > 0 && (
          <p className="text-xs text-gray-600 mb-2" data-testid="final-rejections">Rejections: {state.final_cut_rejections.map((r) => r.reason).join(' | ')}</p>
        )}
        {!state.approvals.g_final_cut && (
          <div className="flex flex-wrap gap-2 items-center">
            {/* Deliberately NOT disabled by the client-side blocker list: the API decides and its 409 is shown. */}
            <button data-testid="approve-final" disabled={busy || gate !== 'G_FINAL_CUT'}
              onClick={() => act('Approve final cut', () => postJson(`/api/episodes/${episodeId}/approve`, { gate_id: 'g_final_cut' }))}
              className={`${btn} bg-green-700 text-white`}>Approve final cut</button>
            <input data-testid="final-reject-reason" value={finalRejectReason} onChange={(e) => setFinalRejectReason(e.target.value)}
              placeholder="Rejection reason" className="px-2 py-1 border rounded text-sm bg-white dark:bg-gray-900" />
            <button data-testid="reject-final" disabled={busy || gate !== 'G_FINAL_CUT' || !finalRejectReason.trim()}
              onClick={() => act('Reject final cut', () => postJson(`/api/episodes/${episodeId}/final-cut/reject`, { reason: finalRejectReason.trim() }).then(() => setFinalRejectReason('')))}
              className={`${btn} bg-red-600 text-white`}>Reject final cut</button>
          </div>
        )}
      </div>
    </div>
  );
}
