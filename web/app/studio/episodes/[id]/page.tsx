'use client';

import { use, useCallback, useEffect, useMemo, useState, type ComponentProps, type FormEvent } from 'react';
import Link from 'next/link';
import { DEFAULT_EPISODE_CAP_USD_MICROS, formatUsd } from '@/lib/money';
import { formatLimits, summarizePreflight } from '@/lib/model-limits.mjs';
import type { ModelView } from '@/lib/model-limits.mjs';
import type { CastView } from '@/lib/cast.mjs';
import type { ShotPlanView } from '@/lib/props.mjs';
import { madeWith, type EpisodeUniverse } from '@/lib/world.mjs';
import {
  STEPS, STEP_LABELS, episodeViewer, liveRun, nextStep, statusChip, stepperSteps, storyDone, storyLocked, waitsOn,
} from '@/lib/next-step.mjs';
import type { StepperStep } from '@/lib/next-step.mjs';
import { stageLabel } from '@/lib/labels';
import { formatLocalDateTime } from '@/lib/time';
import { ApiError, apiFetch, postJson } from '@/lib/studio-api';
import { btnDanger, btnPrimary, btnSecondary, card, chip, focusRing, input, link, panel, tones } from '@/lib/ui';
import CastPanel from '@/components/CastPanel';
import ConceptFrames from '@/components/ConceptFrames';
import PropsPanel from '@/components/PropsPanel';
import { ConfirmButton } from '@/components/ConfirmButton';
import {
  FinalCutSection, FinishLine, RunDetails, RunEndedCard, SceneStillsSection, ShotListDetails, ShotPlanSection,
  StallBanner, problemText, useEpisodeRun,
} from '@/components/EpisodeRunPanel';
import type { Act, ShotPreflight, ShotRow } from '@/components/EpisodeRunPanel';
import NextStepCard, { CostBreakdown, Stepper } from '@/components/NextStepCard';
import { FREE_NOTE, type OfflineEstimate } from '@/lib/checkout.mjs';
import { useMe } from '@/components/StudioShell';
import StoryShapePanel from '@/components/StoryShapePanel';
import { Toast, type Notice } from '@/components/Toast';
import { EpisodeBreadcrumb, LockGateCard, StartCard, UniverseStrip, hasStartKeptHere } from '@/components/UniverseEpisode';
import { GlassPanel } from '@/components/playful';

/**
 * The episode page (DESIGN-TARGETS-v2 §6.5): the step trail (done, now), then, in the order a creator
 * works, what it uses from its Universe (with the lock gate), the story, the cast, the shot plan (G1),
 * the scene pictures (G2), the final cut
 * and the download, beside the gate panel (sticky; a bottom sheet on a phone) that says what happens
 * next. Everything an admin or a curious creator may want to check (credits, live mode, G1.08 and
 * canary, the budget lines, the moderation ladder, QC verdicts, model limits, the raw shot list and
 * the audit trail) sits in the collapsed Production details at the bottom; the budget lines, model
 * limits and admin-only controls render only for admins. Each paid step shows one price first, in a
 * checkout, and what the video cost is one figure with its breakdown behind a disclosure
 * (docs/EASY-CHECKOUT.md). The owner (or an admin) acts; anyone else who may open the episode sees it
 * read-only.
 */

// All budget amounts are integer usd_micros (1 USD = 1_000_000); display with formatUsd()
interface BudgetLine {
  line_name: string;
  provider: string;
  spent: number;
  reserved: number;
  total: number;
  cap: number;
  stop: number;
  at_stop: boolean;
  unit: string;
  revision_reserve?: boolean;
}

interface BudgetStatus {
  episode_id: string;
  lines: BudgetLine[];
  higgsfield_total: number;
  elevenlabs_total: number;
  episode_total?: number;
  episode_cap?: number;
  episode_stop?: number;
  gc01_headroom?: number;
  balance?: {
    manual_balance_usd_micros: number | null;
    set_at: string | null;
    balance_remaining_usd_micros: number | null;
  };
}

interface Gates {
  live_mode: boolean;
  g108_approved: boolean;
}

interface AuditEntry {
  id: number;
  action: string;
  details: string | null;
  user: string;
  timestamp: string;
}

type StoryShapeData = NonNullable<ComponentProps<typeof StoryShapePanel>['data']>;
type StoryOutline = NonNullable<StoryShapeData['story_shape']>;

interface EpisodeRow {
  episode_id: string;
  user_id: string | null;
  owner_name: string | null;
}

type Owner = { userId: string | null; name: string | null };
/**
 * Who made the episode and whether the reader may act on it: GET .../universe, else (when that cannot
 * be read) the episode list, where an episode it does not list leaves the reader unable to act.
 */
type Maker = { from: 'universe'; info: EpisodeUniverse } | { from: 'list'; owner: Owner | null };
type Load = 'loading' | 'ok' | 'missing' | 'error';

const sub = 'rounded-inner border-2 border-line bg-white p-4';
/** The budget lines behind each paid step's row in the cost breakdown. */
const STEP_LINES: Record<string, (line: string) => boolean> = {
  stills: (line) => ['L1_refs', 'L2_drafts', 'L3_final_stills', 'L7_revision'].includes(line),
  final_cut: (line) => line === 'L4_video' || line.startsWith('el_'),
};
const DECISIONS = ['story', 'cast', 'shot_plan', 'stills', 'final_cut'];

function LiveModeCard({ episodeId, gates, canChange, busy, act }: {
  episodeId: string; gates: Gates | null; canChange: boolean; busy: boolean; act: Act;
}) {
  const [asking, setAsking] = useState(false);
  const [typed, setTyped] = useState('');
  const live = Boolean(gates?.live_mode);
  const cancel = () => {
    setAsking(false);
    setTyped('');
  };
  return (
    <div className={sub} data-testid="live-mode-card">
      <h3 className="mb-1 font-semibold">Live mode</h3>
      {live ? (
        <p className="flex items-center gap-2 text-sm font-semibold text-green-800">
          <span aria-hidden="true" className="h-3 w-3 animate-pulse rounded-full bg-green-600" />
          LIVE - Real providers active (every picture and clip is paid for)
        </p>
      ) : (
        <p className="flex items-center gap-2 text-sm text-gray-700">
          <span aria-hidden="true" className="h-3 w-3 rounded-full bg-gray-500" />
          DRY RUN - Fake providers (no spend): a preview with free stand-in pictures
        </p>
      )}
      {!canChange && <p className="mt-2 text-sm text-gray-700">Only the owner or an admin can change this.</p>}
      {canChange && !live && !asking && (
        <button type="button" onClick={() => setAsking(true)} className={`${btnDanger} mt-3`}>Enable Live Mode</button>
      )}
      {canChange && !live && asking && (
        <div className="mt-3 rounded-lg border border-red-200 bg-red-50 p-4" role="group"
          aria-labelledby="live-warning">
          <p id="live-warning" className="mb-3 text-sm text-red-800">
            <strong>Live mode will charge real money (USD).</strong> Type &quot;ENABLE LIVE MODE&quot; to confirm:
          </p>
          <label htmlFor="live-confirm-text" className="sr-only">Type ENABLE LIVE MODE to confirm</label>
          <input id="live-confirm-text" type="text" value={typed} onChange={(e) => setTyped(e.target.value)}
            placeholder="Type here..." className={`${input} mb-2`} autoComplete="off" />
          <div className="flex gap-2">
            <button type="button" disabled={typed !== 'ENABLE LIVE MODE' || busy} className={btnDanger}
              onClick={async () => {
                const ok = await act(() => postJson(`/api/episodes/${episodeId}/set-live`, { confirmation: 'ENABLE_LIVE_MODE' }),
                  'Live mode is on.', "Couldn't enable live mode");
                if (ok) cancel();
              }}>
              Confirm
            </button>
            <button type="button" onClick={cancel} className={btnSecondary}>Cancel</button>
          </div>
        </div>
      )}
      {canChange && live && (
        <div className="mt-3">
          <ConfirmButton className={btnSecondary} disabled={busy} confirmLabel="Switch"
            question="Switch to dry mode? This will disable live generation."
            onConfirm={() => act(() => apiFetch(`/api/episodes/${episodeId}/set-dry`, { method: 'POST' }),
              'Switched to dry mode.', "Couldn't switch to dry mode")}>
            Switch to Dry Mode
          </ConfirmButton>
        </div>
      )}
    </div>
  );
}

function CanaryCard({ episodeId, gates, budget, busy, act, onFinished }: {
  episodeId: string; gates: Gates | null; budget: BudgetStatus | null; busy: boolean; act: Act; onFinished: () => void;
}) {
  const [canary, setCanary] = useState<{ workflowId: string | null; status: string | null; error: string | null }>(
    { workflowId: null, status: null, error: null });
  // Canary cost estimate (usd_micros): the L6 hold reported by the API (0 until a canary runs)
  const estimate = budget?.lines.find((l) => l.line_name === 'L6_reserve')?.reserved ?? 0;
  const running = canary.status === 'starting' || canary.status === 'running';

  useEffect(() => {
    const wf = canary.workflowId;
    if (!wf || canary.status === 'completed' || canary.status === 'failed') return;
    const poll = async () => {
      try {
        const result = await apiFetch<{ status: string; error?: string | null }>(`/api/canary/${wf}`);
        setCanary((prev) => ({ ...prev, status: result.status, error: result.status === 'failed' ? result.error || 'Unknown error' : prev.error }));
        if (result.status === 'completed' || result.status === 'failed') onFinished();
      } catch {
        // keep the last status; the next poll tries again
      }
    };
    poll();
    const t = setInterval(poll, 2000);
    return () => clearInterval(t);
  }, [canary.workflowId, canary.status, onFinished]);

  const start = async () => {
    setCanary({ workflowId: null, status: 'starting', error: null });
    const started: { workflowId: string | null } = { workflowId: null };
    await act(async () => {
      const result = await apiFetch<{ success: boolean; message?: string; workflow_id?: string }>(
        `/api/episodes/${episodeId}/canary`, { method: 'POST' });
      // A refused canary comes back as success: false with the reason
      if (!result.success) throw new ApiError(409, result.message || 'The canary was refused.');
      started.workflowId = result.workflow_id ?? null;
    }, 'Canary started.', "Couldn't start the canary");
    setCanary(started.workflowId
      ? { workflowId: started.workflowId, status: 'running', error: null }
      : { workflowId: null, status: null, error: null });
  };

  const tone = canary.status === 'completed' ? tones.ok : canary.status === 'failed' ? tones.bad : tones.wait;
  return (
    <div className={sub} data-testid="g108-card">
      <h3 className="mb-1 font-semibold">G1.08 credit plan &amp; canary</h3>
      {gates?.g108_approved ? (
        <p className="text-sm font-semibold text-green-800">G1.08 Approved</p>
      ) : (
        <>
          <p className="mb-3 text-sm text-gray-700">The credit plan must be approved before a canary or paid generation.</p>
          <button type="button" disabled={busy} className={btnPrimary} data-testid="approve-g108"
            onClick={() => act(() => apiFetch(`/api/episodes/${episodeId}/approve-g108`, { method: 'POST' }),
              'G1.08 approved.', "Couldn't approve G1.08")}>
            Approve G1.08
          </button>
        </>
      )}
      {gates?.g108_approved && (
        <div className="mt-3 rounded-lg border border-blue-200 bg-blue-50 p-4 text-sm">
          <p className="text-blue-900"><strong>Canary test</strong>: 1 still + 1 clip. Estimated cost: <strong>{formatUsd(estimate)}</strong></p>
          {canary.status && (
            <p className={`mt-2 ${chip} ${tone}`} data-testid="canary-status">Status: {canary.status}</p>
          )}
          {canary.error && <p className="mt-1 text-xs text-red-800">Error: {canary.error}</p>}
          <div className="mt-3">
            <ConfirmButton className={btnPrimary} disabled={busy || running} confirmLabel="Run it"
              question="Run the canary test? It makes 1 still + 1 clip." onConfirm={start}>
              {running ? 'Running...' : 'Run Canary'}
            </ConfirmButton>
          </div>
        </div>
      )}
    </div>
  );
}

function BudgetCard({ budget }: { budget: BudgetStatus | null }) {
  // Episode cap in usd_micros: from the API (hfvg/budget.py settings, default $60.00)
  const episodeCapMicros = budget?.episode_cap ?? DEFAULT_EPISODE_CAP_USD_MICROS;
  return (
    <div className={sub} data-testid="budget-card">
      <h3 className="mb-2 font-semibold">Budget</h3>
      <div className="mb-4 grid gap-4 md:grid-cols-2">
        <div>
          <p className="text-sm text-gray-700">Higgsfield total</p>
          <p className="text-2xl font-bold">
            {formatUsd(budget?.higgsfield_total ?? 0)}
            <span className="ml-2 text-sm font-normal text-gray-700">/ {formatUsd(episodeCapMicros)} episode cap</span>
          </p>
          <p className="mt-1 text-xs text-gray-700">
            Line caps total: {formatUsd(budget?.lines.reduce((sum, l) => l.provider === 'higgsfield' && !l.revision_reserve ? sum + l.cap : sum, 0) ?? 0)}
            {budget?.episode_stop !== undefined && <> • Episode stop (80%): {formatUsd(budget.episode_stop)}</>}
          </p>
          <p className="mt-1 text-xs text-gray-700">
            {budget?.balance?.manual_balance_usd_micros != null
              ? <>Manual balance: {formatUsd(budget.balance.manual_balance_usd_micros)} (left above holds: {formatUsd(budget.balance.balance_remaining_usd_micros ?? 0)}; GC.01 headroom {formatUsd(budget.gc01_headroom ?? 0)})</>
              : <>No manual balance recorded: live paid jobs are refused (GC.01)</>}
          </p>
        </div>
        <div>
          <p className="text-sm text-gray-700">ElevenLabs total (deferred)</p>
          <p className="text-2xl font-bold text-gray-600">{formatUsd(budget?.elevenlabs_total ?? 0)}</p>
        </div>
      </div>
      <ul className="space-y-2">
        {budget?.lines.map((line) => (
          <li key={line.line_name}
            className={`rounded-lg border p-3 ${line.at_stop ? 'border-red-200 bg-red-50' : 'border-gray-200 bg-gray-50'}`}>
            <div className="mb-2 flex items-center justify-between text-sm">
              <span className="font-medium">{line.line_name}</span>
              <span>
                {formatUsd(line.total)} / {formatUsd(line.stop)}
                <span className="ml-1 text-gray-700">(cap {formatUsd(line.cap)}{line.revision_reserve ? ', revision tag only' : ''})</span>
              </span>
            </div>
            <div className="h-1.5 w-full rounded-full bg-gray-200">
              <div className={`h-1.5 rounded-full ${line.at_stop ? 'bg-red-600' : 'bg-green-600'}`}
                style={{ width: `${line.stop ? Math.min((line.total / line.stop) * 100, 100) : 0}%` }} />
            </div>
            <p className="mt-1 text-xs text-gray-700">
              Spent: {formatUsd(line.spent)} • Reserved: {formatUsd(line.reserved)}
              {line.at_stop && <span className="ml-2 font-semibold text-red-700">AT STOP</span>}
            </p>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ModelLimitsCard({ models, capsStale, capsChecked, preflight, preflightError }: {
  models: ModelView[]; capsStale: boolean; capsChecked: string | null;
  preflight: Record<string, ShotPreflight> | null; preflightError: string | null;
}) {
  const summary = preflight ? summarizePreflight(preflight) : null;
  return (
    <div className={sub} data-testid="model-limits-card">
      <h3 className="mb-1 font-semibold">Model limits &amp; preflight</h3>
      <p className="mb-3 text-xs text-gray-700">
        Checked before any estimate or spend; G1 cannot be approved while a shot fails.
        {capsChecked && <> Limits checked {capsChecked}.</>}
        {capsStale && <span className="ml-1 font-semibold text-amber-800">Limits are due for a recheck.</span>}
      </p>
      {preflight && summary ? (
        <div className="mb-4" data-testid="preflight-results">
          <p className={`mb-2 text-sm font-medium ${summary.failed ? 'text-red-700' : 'text-green-800'}`}>
            {summary.failed
              ? `${summary.failed} shot(s) break a model limit, ${summary.passed} pass`
              : `All ${summary.passed} shot(s) pass preflight`}
          </p>
          <ul className="space-y-1">
            {Object.keys(preflight).sort().map((sid) => (
              <li key={sid} className="text-sm" data-testid={`preflight-${sid}`}>
                <span className={preflight[sid].ok ? 'text-green-800' : 'text-red-700'}>
                  {sid}: {preflight[sid].ok ? 'passes' : 'breaks a limit'}
                </span>
                {preflight[sid].errors.map((e, i) => <div key={`e${i}`} className="ml-5 text-xs text-red-700">{e}</div>)}
                {preflight[sid].warnings.map((w, i) => <div key={`w${i}`} className="ml-5 text-xs text-amber-800">{w}</div>)}
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="mb-4 text-sm text-gray-700">{preflightError || 'Upload shots.json to run preflight.'}</p>
      )}
      {models.length > 0 && (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {models.map((m) => (
            <div key={m.model_id} className="min-w-0 rounded border border-gray-200 p-3">
              <p className="mb-2 break-all font-mono text-xs">{m.model_id} <span className="text-gray-700">({m.kind})</span></p>
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <tbody>
                    {formatLimits(m).map((row) => (
                      <tr key={row.label} title={row.note || undefined}>
                        <td className="pr-2 align-top text-gray-700">{row.label}</td>
                        <td className="align-top">{row.value}</td>
                        <td className="pl-2 align-top text-gray-600">{row.source}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function AuditTrail({ audit, hidden }: { audit: AuditEntry[]; hidden: boolean }) {
  return (
    <div className={sub} data-testid="audit-trail">
      <h3 className="mb-2 font-semibold">Audit trail</h3>
      {hidden && audit.length > 0 && (
        <p className="mb-2 text-sm text-gray-700" data-testid="audit-details-hidden">
          Only the people working on this video (and admins) see the details of each change.
        </p>
      )}
      {audit.length === 0 ? (
        <p className="text-sm text-gray-700">No audit entries yet.</p>
      ) : (
        <ul className="space-y-2">
          {audit.map((entry) => (
            <li key={entry.id} className="rounded-lg border border-gray-200 p-3 text-sm">
              <div className="mb-1 flex flex-wrap items-start justify-between gap-2">
                <span className="font-semibold">{entry.action}</span>
                <time className="text-xs text-gray-700" dateTime={entry.timestamp}>
                  {formatLocalDateTime(entry.timestamp) || entry.timestamp}
                </time>
              </div>
              {entry.details && <p className="text-gray-700">{entry.details}</p>}
              <p className="mt-1 text-xs text-gray-700">User: {entry.user}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function UploadFiles({ episodeId, onUploaded }: { episodeId: string; onUploaded: () => Promise<void> }) {
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<{ tone: 'ok' | 'bad'; text: string } | null>(null);
  const [formKey, setFormKey] = useState(0);

  const upload = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setSending(true);
    setResult(null);
    try {
      await apiFetch('/api/episodes/upload', { method: 'POST', body: form });
      setResult({ tone: 'ok', text: 'Files uploaded.' });
      setFormKey((k) => k + 1);
      await onUploaded();
    } catch (err) {
      setResult({ tone: 'bad', text: `The files were not uploaded: ${problemText(err)}` });
    } finally {
      setSending(false);
    }
  };

  const fileField = (name: string, label: string, required = false) => (
    <div>
      <label htmlFor={`upload-${name}`} className="mb-1 block text-sm font-semibold">{label}</label>
      <input id={`upload-${name}`} type="file" name={name} accept=".md" required={required}
        className={`w-full rounded-lg border border-gray-300 px-3 py-2 text-sm ${focusRing}`} />
    </div>
  );

  return (
    <div className={sub} data-testid="upload-files">
      <h3 className="mb-1 font-semibold">Upload episode files</h3>
      <p className="mb-3 text-sm text-gray-700">Replace the beat map, credit plan or continuity notes of this episode.</p>
      <form key={formKey} onSubmit={upload} className="grid gap-3 md:grid-cols-3">
        <input type="hidden" name="episode_id" value={episodeId} />
        {fileField('beatmap_file', 'BEATMAP.md (required)', true)}
        {fileField('credit_plan_file', 'CREDIT-PLAN.md (optional)')}
        {fileField('continuity_file', 'CONTINUITY.md (optional)')}
        <div className="md:col-span-3">
          <button type="submit" disabled={sending} className={btnSecondary}>{sending ? 'Uploading…' : 'Upload'}</button>
        </div>
      </form>
      {result && (
        <p role={result.tone === 'bad' ? 'alert' : 'status'}
          className={`mt-3 rounded p-3 text-sm ${result.tone === 'ok' ? 'bg-green-50 text-green-800' : 'bg-red-50 text-red-800'}`}>
          {result.text}
        </p>
      )}
    </div>
  );
}

export default function EpisodePage({ params }: { params: Promise<{ id: string }> }) {
  const { id: episodeId } = use(params);
  const { me, isAdmin } = useMe();
  const run = useEpisodeRun(episodeId);
  const { refresh: refreshRun } = run;

  const [budget, setBudget] = useState<BudgetStatus | null>(null);
  const [gates, setGates] = useState<Gates | null>(null);
  const [gatesLoad, setGatesLoad] = useState<Load>('loading');
  const [shots, setShots] = useState<ShotRow[]>([]);
  const [storyShape, setStoryShape] = useState<StoryShapeData | null>(null);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [auditHidden, setAuditHidden] = useState(false);
  const [maker, setMaker] = useState<Maker | null>(null);
  const [made, setMade] = useState<Record<string, string[]> | null>(null);
  const [linked, setLinked] = useState(0);
  const [cast, setCast] = useState<CastView | null>(null);
  const [propsChanges, setPropsChanges] = useState(0);
  const [planSaves, setPlanSaves] = useState(0);
  const [frames, setFrames] = useState<{ picked: number; shots: number } | null>(null);
  const [keptHere, setKeptHere] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState(false);
  const [models, setModels] = useState<ModelView[]>([]);
  const [capsStale, setCapsStale] = useState(false);
  const [capsChecked, setCapsChecked] = useState<string | null>(null);
  const [preflight, setPreflight] = useState<Record<string, ShotPreflight> | null>(null);
  const [preflightError, setPreflightError] = useState<string | null>(null);
  const [costEstimate, setCostEstimate] = useState<OfflineEstimate | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);

  useEffect(() => {
    if (!sheetOpen) return;
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setSheetOpen(false); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [sheetOpen]);

  const loadPage = useCallback(async () => {
    const [g, b, s, a, st] = await Promise.allSettled([
      apiFetch<Gates>(`/api/episodes/${episodeId}/gates`),
      apiFetch<BudgetStatus>(`/api/episodes/${episodeId}/budget`),
      apiFetch<{ shots: ShotRow[] }>(`/api/episodes/${episodeId}/shots`),
      apiFetch<{ entries: AuditEntry[]; cast_hidden?: boolean }>(`/api/episodes/${episodeId}/audit`),
      apiFetch<StoryShapeData>(`/api/episodes/${episodeId}/story-shape`),
    ]);
    if (g.status === 'fulfilled') {
      setGates(g.value);
      setGatesLoad('ok');
    } else {
      const missing = g.reason instanceof ApiError && (g.reason.status === 404 || g.reason.status === 400);
      setGatesLoad(missing ? 'missing' : 'error');
    }
    if (b.status === 'fulfilled') setBudget(b.value);
    if (s.status === 'fulfilled') setShots(s.value?.shots ?? []);
    if (a.status === 'fulfilled') {
      setAudit(a.value?.entries ?? []);
      setAuditHidden(Boolean(a.value?.cast_hidden));
    }
    if (st.status === 'fulfilled') setStoryShape(st.value);
  }, [episodeId]);

  // Model limits (GET /api/models) and per-shot preflight of the uploaded shots.json
  // (POST /api/episodes/{id}/preflight): the same checks G1 approval and every submit enforce.
  const loadLimits = useCallback(async () => {
    try {
      const body = await apiFetch<{ models?: ModelView[]; stale?: boolean; checked?: string | null }>('/api/models');
      setModels(body.models || []);
      setCapsStale(Boolean(body.stale));
      setCapsChecked(body.checked || null);
    } catch {
      // the limits card stays empty; preflight still enforces server-side
    }
    try {
      const body = await postJson<{ shots?: Record<string, ShotPreflight>; cost_estimate?: OfflineEstimate }>(
        `/api/episodes/${episodeId}/preflight`, {});
      setPreflight(body.shots || {});
      setCostEstimate(body.cost_estimate ?? null);
      setPreflightError(null);
    } catch (err) {
      setPreflight(null);
      setCostEstimate(null);
      setPreflightError(err instanceof ApiError ? err.message : 'Preflight unavailable');
    }
  }, [episodeId]);

  const { polling } = run;
  useEffect(() => {
    loadPage();
  }, [loadPage]);
  useEffect(() => {
    if (!polling) return;
    const t = setInterval(loadPage, 3000);
    return () => clearInterval(t);
  }, [loadPage, polling]);

  useEffect(() => {
    loadLimits();
  }, [loadLimits]);

  const loadMaker = useCallback(async () => {
    try {
      const info = await apiFetch<EpisodeUniverse>(`/api/episodes/${episodeId}/universe`);
      setMaker({ from: 'universe', info });
    } catch {
      try {
        const r = await apiFetch<{ episodes: EpisodeRow[] }>('/api/episodes');
        const row = (r.episodes ?? []).find((e) => e.episode_id === episodeId);
        setMaker((prev) => prev ?? { from: 'list', owner: row ? { userId: row.user_id, name: row.owner_name } : null });
      } catch {
        setMaker((prev) => prev ?? { from: 'list', owner: null });
      }
    }
  }, [episodeId]);

  useEffect(() => {
    loadMaker();
  }, [loadMaker]);

  const refreshAll = useCallback(async () => {
    await Promise.all([refreshRun(), loadPage(), loadMaker()]);
  }, [refreshRun, loadPage, loadMaker]);
  // joining a Universe moves the episode's cast and props there: the panels load them again
  const onLinked = useCallback(async () => {
    setLinked((n) => n + 1);
    await refreshAll();
  }, [refreshAll]);

  const dismiss = useCallback(() => setNotice(null), []);
  // a lock (or a new character) changes what the episode uses from its Universe and what it waits on
  const onCastView = useCallback((view: CastView) => {
    setCast(view);
    loadMaker();
  }, [loadMaker]);
  const onPropsChange = useCallback(() => setPropsChanges((n) => n + 1), []);
  // a saved shot plan changes what each shot's first frame shows: the first frames load again
  const onPlanSaved = useCallback(async () => {
    setPlanSaves((n) => n + 1);
    await refreshAll();
  }, [refreshAll]);
  // the wizard keeps a start for a video that waits on its page (for a lock, or for its first frames) in
  // the studio (info.kept_start); an earlier wizard kept it in the browser it ran in
  useEffect(() => {
    setKeptHere(hasStartKeptHere(episodeId));
  }, [episodeId]);
  // the shot plan picker reloads when the cast it picks from (or its shots' cast) or the props change
  const castKey = useMemo(() => (cast ? JSON.stringify([
    cast.characters.map((c) => [c.slug, c.name, c.default_version]),
    cast.shots?.shots?.map((s) => [s.shot_id, s.cast]),
  ]) : ''), [cast]);
  const planKey = `${castKey}|${propsChanges}`;

  const act: Act = useCallback(async (fn, done, failed, refused) => {
    setBusy(true);
    setNotice(null);
    let ok = false;
    try {
      await fn();
      setNotice({ tone: 'ok', text: done });
      ok = true;
    } catch (e) {
      if (!(e instanceof ApiError && refused?.(e.message))) {
        setNotice({ tone: 'bad', text: `${failed}: ${problemText(e)}`, status: e instanceof ApiError ? e.status : undefined });
      }
    } finally {
      setBusy(false);
    }
    await refreshAll();
    return ok;
  }, [refreshAll]);

  const handleStoryUpdate = async (shape: StoryOutline) => {
    // PUT .../story-shape takes the outline itself as the body; a refusal shows in the Story panel
    await apiFetch(`/api/episodes/${episodeId}/story-shape`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(shape),
    });
    setNotice({ tone: 'ok', text: 'Story saved.' });
    await refreshAll();
  };

  const handleStoryApprove = async () => {
    await postJson(`/api/episodes/${episodeId}/approve`, { gate_id: 'story_shape' });
    setNotice({ tone: 'ok', text: 'Story approved. Planning the shots now.' });
    await refreshAll();
  };

  const { state, status, stateError, shotStates, shotStatesError, castHidden, credits, stalls, files } = run;
  const info = maker?.from === 'universe' ? maker.info : null;
  // until the reader is known nothing offers them a decision or says it waits for them
  const viewer = useMemo(() => {
    if (!me || !maker) return null;
    if (maker.from === 'universe') return episodeViewer(maker.info, me);
    if (!maker.owner) return { mine: false, canAct: isAdmin, ownerName: null };
    const own = maker.owner.userId === null || maker.owner.userId === me.user_id;
    return { mine: own, canAct: isAdmin || own, ownerName: maker.owner.name };
  }, [me, maker, isAdmin]);
  const canAct = viewer?.canAct ?? false;
  const owner = info ? { userId: info.owner.user_id, name: info.owner.name } : maker?.from === 'list' ? maker.owner : null;
  const ownerText = !owner || owner.userId === null ? null
    : owner.userId === me?.user_id ? 'Made by you' : `Made by ${owner.name || 'another member'}`;

  // what each scene picture was made with, for the owner (or an admin): the shot plan carries its prompts
  const hasRun = Boolean(state);
  useEffect(() => {
    if (!canAct || !hasRun) return;
    let cancelled = false;
    apiFetch<ShotPlanView>(`/api/episodes/${episodeId}/shot-plan`)
      .then((plan) => {
        if (!cancelled) setMade(madeWith(plan));
      })
      .catch(() => {
        if (!cancelled) setMade(null);
      });
    return () => {
      cancelled = true;
    };
  }, [episodeId, canAct, hasRun, planKey]);

  if (status === 'loading' || (status === 'missing' && gatesLoad === 'loading')) {
    return (
      <div className="mx-auto max-w-5xl px-4 py-24 text-center sm:px-6" role="status">
        <div aria-hidden="true" className="mx-auto mb-4 h-10 w-10 animate-spin rounded-full border-b-2 border-gray-900" />
        <p className="text-gray-700">Loading {episodeId}…</p>
      </div>
    );
  }

  if (status === 'missing' && gatesLoad === 'missing') {
    return (
      <div className="mx-auto max-w-2xl px-4 py-16 sm:px-6">
        <div className={`${card} text-center`} data-testid="episode-not-found">
          <h1 className="text-2xl font-bold">We couldn&apos;t find this video</h1>
          <p className="mt-2 text-gray-700">
            There is no video called <span className="font-mono">{episodeId}</span>. Check the link, or pick a video from your list.
          </p>
          <div className="mt-6 flex flex-wrap justify-center gap-3">
            <Link href="/studio" className={btnPrimary}>Back to the studio</Link>
            <Link href="/studio/create" className={btnSecondary}>Make a new video</Link>
          </div>
        </div>
      </div>
    );
  }

  const story = storyShape?.story_shape ? storyShape : null;
  const waitingOn = waitsOn(viewer);
  const next = state
    ? nextStep(state, storyShape, cast, { files, shots: shotList(shotStates), viewer })
    : nextStep(null, null, null, { files, viewer });
  const steps = state ? stepperSteps(state, storyShape, cast, next) : null;
  // the run's stage stays where it was while a step waits for a decision: the chip says the step instead
  const headerChip = next.step === 'working' ? { label: stageLabel(state?.stage), tone: 'wait' as const } : statusChip(next);
  const completed = Boolean(state?.run?.ended && state.run.outcome === 'completed');
  // the run's history is gone and nothing saved shows its steps: only the files it left are shown
  const expired = next.step === 'expired';
  const notStarted = status === 'missing';
  const storyIsDone = storyDone(state) || Boolean(storyShape?.approved);
  const storyReadOnly = storyLocked(state) || !canAct;
  const storyNote = !canAct
    ? 'Only the person who made this video, or an admin, can change the story.'
    : state?.run?.ended ? 'This run has ended, so the story can no longer change.' : null;
  const storyText = expired
    ? 'The story outline of this run is no longer available.'
    : !state
      ? 'The story outline shows up here once the video starts.'
      : storyIsDone
        ? 'This video was made from uploaded files, without a story outline to approve.'
        : 'The studio is writing the story outline. It shows up here for you to read and approve.';

  const live = Boolean(gates?.live_mode);
  const dryRun = state?.run?.dry_run ?? null;
  const runLive = liveRun({ live, dryRun });
  const spent = budget ? budget.lines.reduce((sum, line) => sum + line.spent, 0) : null;
  // a step's spend is on record only when the episode's budget has lines of its own for it
  const costs = budget ? Object.fromEntries(Object.entries(STEP_LINES).map(([key, of]) => {
    const lines = budget.lines.filter((line) => of(line.line_name));
    return [key, lines.length > 0 ? lines.reduce((sum, line) => sum + line.spent, 0) : null];
  })) : null;
  // a finished live run that recorded no spend says so; "Free" only for a recorded $0 in a test run
  const realCost = spent === null ? '—' : spent === 0 ? (dryRun === true ? 'Free' : 'Not recorded') : formatUsd(spent);
  const kept = Boolean(info?.kept_start) || keptHere;
  const startHere = notStarted && canAct && Boolean(info) && (Boolean(info?.universe) || kept);
  const unlocked = info?.lock?.unlocked ?? [];
  const decision = Boolean(viewer) && !completed && !expired && canAct && !next.waiting && DECISIONS.includes(next.step);
  const sheet = startHere || decision;
  const sheetTitle = startHere ? `Start this video · ${unlocked.length ? 'lock first' : 'ready'}` : next.title;
  // before the run starts there is no run to read the steps from: the trail says where it waits
  const preStart: StepperStep[] | null = startHere ? STEPS.map((key) => ({
    key, label: STEP_LABELS[key], status: key === 'cast' && unlocked.length > 0 ? 'current' : 'todo',
  })) : null;
  const gate = startHere && info ? (
    <StartCard episodeId={episodeId} info={info} frames={frames} price={costEstimate} onStarted={refreshAll} />
  ) : viewer && !completed && !expired ? (
    <NextStepCard next={next} steps={steps} canAct={canAct} live={runLive} spent={spent} costs={costs} dryRun={dryRun} />
  ) : completed ? (
    <section aria-labelledby="cost-title">
      <div>
        <span className={`${chip} ${tones.ok}`}>Finished</span>
        <h2 id="cost-title" className="mt-1 text-[20px]">What it cost</h2>
      </div>
      <p className="mt-2 font-display text-[28px] font-bold leading-tight text-ink" data-testid="episode-real-cost">{realCost}</p>
      {!runLive && <p className="mt-1 text-[13px] text-ink-soft">{FREE_NOTE}</p>}
      <CostBreakdown steps={steps} costs={costs} live={live} dryRun={dryRun} />
    </section>
  ) : null;

  return (
    <div className={`mx-auto max-w-[1240px] px-4 pt-4 sm:px-8 sm:pt-6 ${sheet ? 'pb-48 lg:pb-12' : 'pb-12'}`}>
      <header>
        <EpisodeBreadcrumb episodeId={episodeId} info={info} canAct={canAct} onLinked={onLinked} />
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
          <h1 className="break-all text-[30px] leading-tight sm:text-[40px]">{episodeId}</h1>
          {state && <span className={`${chip} ${tones[headerChip.tone]}`} data-testid="episode-status">{headerChip.label}</span>}
          {ownerText && <span className="text-[13px] font-bold text-ink-soft" data-testid="episode-owner">{ownerText}</span>}
        </div>
        {story?.story_shape?.logline && (
          <p className="mt-1 max-w-[70ch] text-[15px] text-ink-soft">{story.story_shape.logline}</p>
        )}
      </header>

      {stateError && (
        <p role="alert" className="mt-4 rounded-inner bg-coral-soft p-3 text-[13.5px] font-bold text-coral-ink">
          The latest state could not be loaded: {stateError}
        </p>
      )}

      {(steps || preStart) && (
        <section className="pf-card mt-4 px-3 pb-2 pt-4 sm:px-6" aria-label="Steps of this video">
          {steps ? <Stepper steps={steps} />
            : preStart && <Stepper steps={preStart} testId="stepper-before-start" />}
        </section>
      )}

      <div className="mt-5 grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_380px]">
        {gate && sheet && sheetOpen && (
          <div aria-hidden="true" className="fixed inset-0 z-20 bg-ink/40 lg:hidden" onClick={() => setSheetOpen(false)}
            data-testid="gate-sheet-scrim" />
        )}
        {gate && (
          <aside aria-label="What happens next" data-testid="gate-panel"
            className={`lg:sticky lg:top-24 lg:col-start-2 lg:row-start-1 ${sheet ? 'fixed inset-x-0 bottom-0 z-30 lg:inset-auto lg:z-auto' : ''}`}>
            {/* below lg the sheet is solid and reaches the screen's bottom edge, under the phone's bottom
                bar (hidden from 640 px): pb-[88px] keeps its content above that bar */}
            <GlassPanel className={`p-4 sm:p-5 ${sheet ? 'max-lg:rounded-b-none max-lg:border-x-0 max-lg:border-b-0 max-lg:bg-paper max-lg:pb-[88px] max-lg:shadow-[0_-10px_30px_rgba(29,35,48,0.18)] sm:max-lg:pb-5' : ''}`}
              data-testid="gate-sheet">
              {sheet && (
                <>
                  <span aria-hidden="true" className="mx-auto -mt-1 mb-2 block h-1 w-10 rounded-full bg-ink/15 lg:hidden" />
                  <button type="button" className="flex min-h-11 w-full items-center justify-between gap-3 text-left lg:hidden"
                    aria-expanded={sheetOpen} aria-controls="gate-body" onClick={() => setSheetOpen((open) => !open)}
                    data-testid="gate-sheet-toggle">
                    <span className="min-w-0 truncate text-[14px] font-extrabold">{sheetTitle}</span>
                    <span className="pf-btn pf-btn-soft pf-btn-sm shrink-0">{sheetOpen ? 'Close' : 'Open'}</span>
                  </button>
                </>
              )}
              <div id="gate-body"
                className={sheet ? (sheetOpen ? 'mt-3 max-h-[60vh] overflow-y-auto lg:mt-0 lg:max-h-none lg:overflow-visible' : 'hidden lg:block') : ''}>
                {gate}
              </div>
            </GlassPanel>
          </aside>
        )}

        <div className="min-w-0 space-y-6 lg:col-start-1 lg:row-start-1">
          {(completed || expired) && (
            <FinishLine episodeId={episodeId} run={state?.run ?? null} files={files} mine={!viewer || viewer.mine} />
          )}
          {state && <RunEndedCard episodeId={episodeId} state={state} busy={busy} act={act} canAct={canAct} />}
          <StallBanner state={state} stalls={stalls} />
          <UniverseStrip info={info}>{startHere && info && <LockGateCard info={info} />}</UniverseStrip>

          <section id="story" className="scroll-mt-24" aria-label="Story">
            {story ? (
              <StoryShapePanel episodeId={episodeId} data={story} onApprove={handleStoryApprove} onUpdate={handleStoryUpdate}
                readOnly={storyReadOnly} approved={storyIsDone} note={storyNote} />
            ) : (
              <div className={card}>
                <h2 className="text-xl font-semibold">Story</h2>
                <p className="mt-1 text-gray-700">{storyText}</p>
              </div>
            )}
          </section>

          <section id="cast" className="scroll-mt-24" aria-label="Cast">
            <CastPanel key={linked} episodeId={episodeId} onView={onCastView} />
          </section>

          <section id="props" className="scroll-mt-24" aria-label="Props and places">
            <PropsPanel key={linked} episodeId={episodeId} onChange={onPropsChange} />
          </section>

          {(state || notStarted) && !expired && viewer && (
            <ShotPlanSection episodeId={episodeId} state={state} shots={shots} castHidden={castHidden} busy={busy} act={act}
              canAct={canAct} waitingOn={waitingOn} notStarted={notStarted} planKey={planKey} onPlanSaved={onPlanSaved}
              price={costEstimate} live={runLive} />
          )}
          {(state || notStarted) && !expired && viewer && (
            <ConceptFrames episodeId={episodeId} onCount={setFrames}
              refreshKey={`${planKey}|${planSaves}|${linked}|${state?.run?.run_id ?? ''}|${state?.run?.ended ? 'ended' : ''}`} />
          )}
          {state && !expired && viewer && (
            <SceneStillsSection episodeId={episodeId} state={state} shotStates={shotStates} shotStatesError={shotStatesError}
              shots={shots} castHidden={castHidden} busy={busy} act={act} canAct={canAct} waitingOn={waitingOn}
              madeWith={canAct ? made : null} price={costEstimate} live={runLive} />
          )}
          {state && !expired && viewer && (
            <FinalCutSection episodeId={episodeId} state={state} shotStates={shotStates} files={files} busy={busy} act={act}
              canAct={canAct} waitingOn={waitingOn} price={costEstimate} live={runLive} />
          )}

          <details id="production-details" data-testid="production-details" className={`${panel} scroll-mt-4`}>
            <summary className={`cursor-pointer list-none rounded-lg p-4 sm:p-6 ${focusRing}`}>
              <span className="text-lg font-semibold">Production details</span>
              <span className="mt-1 block text-sm text-gray-700">
                {isAdmin
                  ? 'Credits, live mode, budget, checks, model limits, the shot list and the audit trail.'
                  : 'Credits, live mode, checks, the shot list and the audit trail.'}
              </span>
            </summary>
            <div className="space-y-4 border-t border-gray-200 p-4 sm:p-6">
              {state ? (
                <RunDetails episodeId={episodeId} state={state} shotStates={shotStates} credits={credits} stalls={stalls}
                  busy={busy} act={act} isAdmin={isAdmin} />
              ) : (
                <p className="text-sm text-gray-700">
                  {expired ? 'The run state could not be loaded.' : 'No run yet: this episode has not been started.'}
                </p>
              )}
              <LiveModeCard episodeId={episodeId} gates={gates} canChange={canAct} busy={busy} act={act} />
              {isAdmin && (
                <CanaryCard episodeId={episodeId} gates={gates} budget={budget} busy={busy} act={act} onFinished={loadPage} />
              )}
              {isAdmin && (
                <>
                  <BudgetCard budget={budget} />
                  <ModelLimitsCard models={models} capsStale={capsStale} capsChecked={capsChecked} preflight={preflight}
                    preflightError={preflightError} />
                </>
              )}
              <ShotListDetails episodeId={episodeId} state={state} shotStates={shotStates} shots={shots} castHidden={castHidden}
                preflight={preflight} />
              <AuditTrail audit={audit} hidden={auditHidden} />
              {canAct && (
                <UploadFiles episodeId={episodeId} onUploaded={async () => { await loadLimits(); await loadPage(); }} />
              )}
            </div>
          </details>

        </div>
      </div>

      <Toast notice={notice} onDismiss={dismiss} testId="run-message" />
    </div>
  );
}

function shotList(shotStates: Record<string, { still_status?: string; pre_approved?: boolean }> | null) {
  if (!shotStates) return null;
  return Object.entries(shotStates).map(([shot_id, s]) => ({ shot_id, still_status: s.still_status, pre_approved: s.pre_approved }));
}
