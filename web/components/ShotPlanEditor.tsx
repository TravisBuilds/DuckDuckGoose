'use client';

import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { API_URL, ApiError, apiFetch, postJson } from '@/lib/studio-api';
import {
  castSlug, draftProblem, draftValue, historyText, historyValue, planStateText, savedText, shotChanges,
  shotPlanChanges, toggleCastEntry, togglePick,
} from '@/lib/props.mjs';
import type { ShotDraft, ShotPlanSaveResponse, ShotPlanShot, ShotPlanView } from '@/lib/props.mjs';
import { formatLocalDateTime } from '@/lib/time';
import { btnPrimary, btnSecondary, chip, focusRing, input as field, link, tones } from '@/lib/ui';

/**
 * Each shot's place, props and cast, and its prompts, until the shot plan is approved
 * (api/shot_plan_routes.py): a picked prop or place sends its photos with the shot and adds its
 * description to the shot's prompt. While the run waits for the shot plan approval, a save is checked
 * again by the run (model limits, prices) before it counts; after the approval the plan is locked.
 * Every saved change is kept in the shot's history. Only the episode's owner or an admin may save.
 */

type Drafts = Record<string, Partial<ShotDraft>>;
type Note = { tone: 'ok' | 'problem'; text: string };

const UNREACHABLE = 'The studio could not be reached. Check your connection and try again.';
const hint = 'text-xs text-gray-600 dark:text-gray-300';

function problemText(e: unknown): string {
  return e instanceof ApiError ? e.message : UNREACHABLE;
}

function PickChip({ label, thumb, pressed, disabled, onClick, testId }: {
  label: string; thumb: string | null; pressed: boolean; disabled: boolean; onClick: () => void; testId: string;
}) {
  return (
    <button type="button" aria-pressed={pressed} disabled={disabled} onClick={onClick} data-testid={testId}
      className={`inline-flex min-h-10 max-w-full items-center gap-2 rounded-full border px-3 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${focusRing} ${pressed
        ? 'border-blue-600 bg-blue-600 text-white dark:border-blue-400 dark:bg-blue-500'
        : 'border-gray-300 bg-white text-gray-900 hover:bg-gray-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100 dark:hover:bg-gray-700'}`}>
      {thumb ? (
        <img src={`${API_URL}${thumb}`} alt="" className="h-6 w-6 shrink-0 rounded-full object-cover" />
      ) : (
        <span aria-hidden="true" className="h-6 w-6 shrink-0 rounded-full border border-dashed border-current opacity-60" />
      )}
      <span className="truncate">{label}</span>
      {pressed && <span className="sr-only">(picked)</span>}
    </button>
  );
}

function PickRow({ label, note, children }: { label: string; note?: string; children: ReactNode }) {
  return (
    <div className="mt-3">
      <p className="text-xs font-semibold uppercase tracking-wide text-gray-600 dark:text-gray-300">
        {label}{note && <span className="ml-1 font-normal normal-case tracking-normal">{note}</span>}
      </p>
      <div className="mt-1 flex flex-wrap gap-2">{children}</div>
    </div>
  );
}

function PromptField({ id, label, help, value, onChange, max, testId, rows = 4, disabled = false }: {
  id: string; label: string; help?: string; value: string; onChange: (v: string) => void; max: number; testId: string;
  rows?: number; disabled?: boolean;
}) {
  return (
    <div className="mt-3">
      <label htmlFor={id} className="block text-sm font-medium">{label}</label>
      {help && <p id={`${id}-help`} className={hint}>{help}</p>}
      <textarea id={id} value={value} onChange={(e) => onChange(e.target.value)} maxLength={max} rows={rows}
        disabled={disabled} aria-describedby={help ? `${id}-help` : undefined} className={`${field} mt-1`}
        data-testid={testId} />
      <p className={`${hint} text-right`}>{value.length} / {max}</p>
    </div>
  );
}

/** saving: a save is on its way; the save clears the drafts it sent, so nothing can change until it is back. */
function ShotCard({ view, shot, draft, canEdit, saving, editing, onEditing, onChange }: {
  view: ShotPlanView; shot: ShotPlanShot; draft: Partial<ShotDraft> | undefined; canEdit: boolean; saving: boolean;
  editing: boolean; onEditing: (open: boolean) => void; onChange: (change: Partial<ShotDraft>) => void;
}) {
  const sid = shot.shot_id;
  const place = draftValue(shot, draft, 'place');
  const props = draftValue(shot, draft, 'props');
  const cast = draftValue(shot, draft, 'cast');
  const prompt = draftValue(shot, draft, 'prompt');
  const changed = Object.keys(shotChanges(shot, draft)).length > 0;
  const places = view.props.filter((p) => p.kind === 'place');
  const things = view.props.filter((p) => p.kind === 'prop');
  const names: Record<string, string> = Object.fromEntries([
    ...view.props.map((p) => [p.slug, p.name]), ...view.characters.map((c) => [c.slug, c.name]),
  ]);
  const maxProps = view.limits.max_props_per_shot;
  const maxCast = view.limits.max_cast_per_shot;
  const unreadable = shot.cast === null || shot.props === null;
  const disabled = !canEdit || unreadable || saving;
  const facts = [shot.duration ? `${shot.duration} s` : null, shot.model].filter(Boolean).join(' · ');
  return (
    <li className="rounded-lg border border-gray-200 p-3 sm:p-4 dark:border-gray-700" data-testid={`plan-shot-${sid}`}>
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="font-mono text-base font-semibold">{sid}</h3>
        {facts && <span className={hint}>{facts}</span>}
        {changed && <span className={`${chip} ${tones.warn}`} data-testid={`plan-changed-${sid}`}>Not saved yet</span>}
      </div>

      {editing ? (
        <div>
          <PromptField id={`plan-prompt-${sid}`} label="Picture prompt" value={prompt} max={view.limits.prompt_max}
            help="What the scene picture shows. The picked place, props and cast are added to it when it is made."
            onChange={(v) => onChange({ prompt: v })} testId={`plan-prompt-${sid}`} disabled={saving} />
          {shot.end_frame && (
            <PromptField id={`plan-end-prompt-${sid}`} label="End picture prompt" value={draftValue(shot, draft, 'end_prompt')}
              max={view.limits.prompt_max} help="What the last frame of the clip shows."
              onChange={(v) => onChange({ end_prompt: v })} testId={`plan-end-prompt-${sid}`} rows={3} disabled={saving} />
          )}
          <PromptField id={`plan-clip-prompt-${sid}`} label="Motion prompt (optional)" value={draftValue(shot, draft, 'clip_prompt')}
            max={view.limits.prompt_max} help="How the clip moves. Leave it empty to use the picture prompt."
            onChange={(v) => onChange({ clip_prompt: v })} testId={`plan-clip-prompt-${sid}`} rows={2} disabled={saving} />
          <button type="button" className={`${btnSecondary} mt-2`} onClick={() => onEditing(false)}
            data-testid={`plan-edit-done-${sid}`}>
            Done editing
          </button>
        </div>
      ) : (
        <div className="mt-2">
          <p className="whitespace-pre-line break-words text-sm text-gray-800 dark:text-gray-100" data-testid={`plan-prompt-text-${sid}`}>
            {prompt || 'No picture prompt yet.'}
          </p>
          {canEdit && (
            <button type="button" className={`${link} mt-1 text-sm font-semibold`} onClick={() => onEditing(true)}
              data-testid={`plan-edit-${sid}`}>
              Edit the prompt
            </button>
          )}
        </div>
      )}

      {unreadable && (
        <p className="mt-3 text-sm text-red-700 dark:text-red-300">
          This shot’s picks in shots.json cannot be read; fix the file (see the problems above) to change them here.
        </p>
      )}
      {places.length > 0 && (
        <PickRow label="Place" note="(one)">
          {places.map((p) => (
            <PickChip key={p.slug} label={p.name} thumb={p.thumb_url} pressed={place === p.slug} disabled={disabled}
              testId={`plan-place-${sid}-${p.slug}`} onClick={() => onChange({ place: place === p.slug ? '' : p.slug })} />
          ))}
        </PickRow>
      )}
      {things.length > 0 && (
        <PickRow label="Props" note={`(up to ${maxProps})`}>
          {things.map((p) => {
            const picked = (props ?? []).includes(p.slug);
            return (
              <PickChip key={p.slug} label={p.name} thumb={p.thumb_url} pressed={picked}
                disabled={disabled || (!picked && (props ?? []).length >= maxProps)} testId={`plan-prop-${sid}-${p.slug}`}
                onClick={() => onChange({ props: togglePick(props ?? [], p.slug, maxProps) })} />
            );
          })}
        </PickRow>
      )}
      {view.characters.length > 0 && (
        <PickRow label="Cast" note={`(up to ${maxCast})`}>
          {view.characters.map((c) => {
            const picked = (cast ?? []).some((e) => castSlug(e) === c.slug);
            return (
              <PickChip key={c.slug} label={c.name} thumb={c.thumb_url} pressed={picked}
                disabled={disabled || (!picked && (cast ?? []).length >= maxCast)} testId={`plan-cast-${sid}-${c.slug}`}
                onClick={() => onChange({ cast: toggleCastEntry(cast ?? [], c.slug, maxCast) })} />
            );
          })}
        </PickRow>
      )}

      <p className={`mt-3 ${hint}`} data-testid={`plan-refs-${sid}`}>
        {changed
          ? 'Save to see the photos this shot will be made with.'
          : shot.refs.length
            ? `Made with: ${shot.refs.join(' · ')}`
            : 'No reference photos for this shot.'}
      </p>
      {shot.errors.map((e) => (
        <p key={e} className="mt-1 text-sm text-red-700 dark:text-red-300" data-testid={`plan-shot-error-${sid}`}>{e}</p>
      ))}
      {shot.notes.map((n) => <p key={n} className={`mt-1 ${hint}`}>{n}</p>)}

      {shot.history.length > 0 && (
        <details className="mt-3" data-testid={`plan-history-${sid}`}>
          <summary className={`cursor-pointer rounded text-sm font-semibold text-gray-800 dark:text-gray-100 ${focusRing}`}>
            History ({shot.history.length})
          </summary>
          <ol className="mt-2 space-y-2">
            {shot.history.map((h) => (
              <li key={h.id} className="rounded-lg bg-gray-50 p-2 text-sm dark:bg-gray-900/40" data-testid={`plan-history-entry-${sid}`}>
                <p className="font-medium">{historyText(h)}</p>
                <p className={hint}>{formatLocalDateTime(h.at)}{h.note ? ` · “${h.note}”` : ''}</p>
                <p className="mt-1 break-words text-xs text-gray-700 dark:text-gray-200">
                  <span className="font-semibold">Before:</span> {historyValue(h.field, h.old, names)}
                </p>
                <p className="break-words text-xs text-gray-700 dark:text-gray-200">
                  <span className="font-semibold">After:</span> {historyValue(h.field, h.new, names)}
                </p>
              </li>
            ))}
          </ol>
        </details>
      )}
    </li>
  );
}

/**
 * refreshKey: changes when the cast or the props and places change elsewhere on the page, so the
 * picker shows them; unsaved edits are kept. onDirty: whether there are unsaved edits (the shot plan
 * approval waits for them). onSaved: after a save, so the page reloads the run.
 */
export default function ShotPlanEditor({ episodeId, refreshKey, onDirty, onSaved }: {
  episodeId: string; refreshKey?: string; onDirty?: (dirty: boolean) => void; onSaved?: () => void;
}) {
  const [view, setView] = useState<ShotPlanView | null>(null);
  const [loadIssue, setLoadIssue] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Drafts>({});
  const [editing, setEditing] = useState<Record<string, boolean>>({});
  const [note, setNote] = useState('');
  const [message, setMessage] = useState<Note | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setView(await apiFetch<ShotPlanView>(`/api/episodes/${episodeId}/shot-plan`));
      setLoadIssue(null);
    } catch (e) {
      setLoadIssue(e instanceof ApiError ? e.message : 'The shot plan could not be loaded right now.');
    }
  }, [episodeId]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const body = view ? shotPlanChanges(view.shots, drafts, note) : null;
  const dirty = Boolean(body);
  useEffect(() => {
    onDirty?.(dirty);
  }, [dirty, onDirty]);

  if (!view) {
    return <p className="mt-3 text-sm text-gray-700 dark:text-gray-200" data-testid="plan-loading">{loadIssue ?? 'Loading the shots…'}</p>;
  }

  const canEdit = view.can_edit && !view.phase.replanning;
  const problem = draftProblem(view.shots, drafts);
  const change = (sid: string, edit: Partial<ShotDraft>) => {
    setDrafts((d) => ({ ...d, [sid]: { ...d[sid], ...edit } }));
    setMessage(null);
  };
  const undo = () => {
    setDrafts({});
    setEditing({});
    setNote('');
    setMessage(null);
  };
  const save = async () => {
    if (!body || problem) return;
    setBusy(true);
    setMessage(null);
    try {
      const saved = await postJson<ShotPlanSaveResponse>(`/api/episodes/${episodeId}/shot-plan`, body);
      setView(saved);
      setDrafts({});
      setEditing({});
      setNote('');
      setMessage(savedText(saved) ?? { tone: 'ok', text: 'Nothing changed.' });
      onSaved?.();
    } catch (e) {
      setMessage({ tone: 'problem', text: problemText(e) });
      await load();
    } finally {
      setBusy(false);
    }
  };

  const changedShots = body ? Object.keys(body.shots).length : 0;
  return (
    <div className="mt-4" data-testid="shot-plan-editor" data-phase={view.phase.state} data-can-edit={view.can_edit ? 'true' : 'false'}>
      <p className="text-sm text-gray-700 dark:text-gray-200" data-testid="plan-state">{planStateText(view)}</p>
      {view.phase.replanning && (
        <p className="mt-2 text-sm text-amber-800 dark:text-amber-300" role="status">Checking the last change to the shot plan…</p>
      )}
      {view.uploaded && view.props.length === 0 && view.can_edit && (
        <p className="mt-2 rounded-lg bg-gray-50 p-3 text-sm text-gray-700 dark:bg-gray-900/40 dark:text-gray-200" data-testid="plan-no-props">
          No props or places to pick yet. Add them under <a href="#props" className={link}>Props &amp; places</a> with a photo,
          then pick them here for each shot.
        </p>
      )}
      {view.errors.length > 0 && (
        <ul className="mt-3 space-y-1 rounded-lg bg-red-50 p-3 text-sm text-red-800 dark:bg-red-900/20 dark:text-red-200" data-testid="plan-errors">
          {view.errors.map((e) => <li key={e}>{e}</li>)}
        </ul>
      )}
      {view.shots.length > 0 && (
        <ol className="mt-3 space-y-3">
          {view.shots.map((shot) => (
            <ShotCard key={shot.shot_id} view={view} shot={shot} draft={drafts[shot.shot_id]} canEdit={canEdit} saving={busy}
              editing={Boolean(editing[shot.shot_id])} onEditing={(open) => setEditing((e) => ({ ...e, [shot.shot_id]: open }))}
              onChange={(edit) => change(shot.shot_id, edit)} />
          ))}
        </ol>
      )}
      <p role="status" aria-live="polite" className="sr-only">{message?.text ?? ''}</p>
      {canEdit && (
        <div className={`mt-4 rounded-lg border p-3 ${dirty ? 'sticky bottom-2 z-10 border-blue-300 bg-white shadow-lg dark:border-blue-700 dark:bg-gray-800' : 'border-gray-200 dark:border-gray-700'}`}
          data-testid="plan-save-bar">
          <label htmlFor="plan-note" className="block text-sm font-medium">Why the change (optional)</label>
          <input id="plan-note" value={note} onChange={(e) => setNote(e.target.value)} maxLength={view.limits.note_max} disabled={busy}
            placeholder="e.g. the umbrella looked wrong" className={`${field} mt-1`} data-testid="plan-note" />
          {problem && <p className="mt-2 text-sm text-red-700 dark:text-red-300" data-testid="plan-problem">{problem}</p>}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button type="button" className={btnPrimary} disabled={busy || !dirty || Boolean(problem)} onClick={save}
              data-testid="plan-save">
              {busy ? 'Saving…' : dirty ? `Save ${changedShots === 1 ? '1 shot' : `${changedShots} shots`}` : 'Save changes'}
            </button>
            <button type="button" className={btnSecondary} disabled={busy || !dirty} onClick={undo} data-testid="plan-undo">
              Undo
            </button>
            {!dirty && <span className={hint}>Pick a place or props, or edit a prompt, then save.</span>}
          </div>
        </div>
      )}
      {message && (
        <p data-testid="plan-message" data-tone={message.tone}
          className={`mt-3 rounded-lg p-3 text-sm ${message.tone === 'ok' ? 'bg-green-50 text-green-800 dark:bg-green-900/20 dark:text-green-200' : 'bg-red-50 text-red-800 dark:bg-red-900/20 dark:text-red-200'}`}>
          {message.text}
        </p>
      )}
    </div>
  );
}
