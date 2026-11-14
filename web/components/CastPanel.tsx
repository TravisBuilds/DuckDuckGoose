'use client';

import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { API_URL, ApiError, apiFetch, postJson } from '@/lib/studio-api';
import {
  PICTURE_ACCEPT,
  PICTURE_LABELS,
  SHEET_STEP_TEXT,
  anyActiveJobs,
  castPicks,
  defaultOptionCount,
  emptyRights,
  estimateText,
  optionCounts,
  pictureProblem,
  requestProblems,
  rightsFields,
  rightsProblem,
  setCastVersion,
  sheetHasPlaceholders,
  sheetStep,
  shotCastChanges,
  toggleCast,
  versionPictures,
} from '@/lib/cast.mjs';
import type {
  CastCharacter,
  CastImage,
  CastPick,
  CastVersion,
  CastView,
  RightsAnswers,
  SeriesInfo,
  SheetEstimate,
} from '@/lib/cast.mjs';

/**
 * The episode's cast: who is in the video, their pictures and locked character sheets, and who is
 * in each shot. Every action is an API route (api/cast_routes.py), which stays the authority: a
 * refused action shows the API's reason. Paid sheet pictures show their estimate first and are made
 * only for the amount the creator approved (the worker checks the estimate, balance and caps again).
 */

const card = 'bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 mb-6';
const btn = 'px-3 py-1.5 rounded text-sm font-semibold disabled:opacity-50';
const plainBtn = `${btn} border border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700`;
const darkBtn = `${btn} bg-black dark:bg-white text-white dark:text-black`;
const field = 'w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900 text-sm';
const UNREACHABLE = 'The studio could not be reached. Check your connection and try again.';

type Act = <T>(fn: () => Promise<T>, done?: string) => Promise<T | null>;

function problemText(e: unknown): string {
  return e instanceof ApiError ? e.message : UNREACHABLE;
}

function megabytes(bytes: number): number {
  return Math.round(bytes / 1_048_576);
}

function Thumb({ image, label }: { image: CastImage; label?: string }) {
  const text = label ?? PICTURE_LABELS[image.role] ?? image.role;
  return (
    <a href={`${API_URL}${image.url}`} target="_blank" rel="noreferrer" className="block" title="Open the full picture">
      <img
        src={`${API_URL}${image.thumb_url}`}
        alt={text}
        loading="lazy"
        className="w-28 h-28 object-cover rounded border border-gray-300 dark:border-gray-600 bg-gray-100 dark:bg-gray-900"
      />
    </a>
  );
}

function PictureTile({ image, label, children }: { image: CastImage; label?: string; children?: ReactNode }) {
  return (
    <figure className="w-28" data-testid={`cast-image-${image.image_id}`}>
      <Thumb image={image} label={label} />
      <figcaption className="mt-1 text-xs text-gray-600 dark:text-gray-400">
        {label ?? PICTURE_LABELS[image.role] ?? image.role}
        {image.placeholder && <span className="block text-amber-700 dark:text-amber-300">stand-in (preview)</span>}
      </figcaption>
      {children && <div className="mt-1 flex flex-col gap-1">{children}</div>}
    </figure>
  );
}

function PictureUpload({ episodeId, character, version, view, busy, act }: {
  episodeId: string; character: CastCharacter; version: CastVersion; view: CastView; busy: boolean; act: Act;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [rights, setRights] = useState<RightsAnswers>(emptyRights());
  const [inputKey, setInputKey] = useState(0);
  const limits = view.limits;
  const fileIssue = file ? pictureProblem(file, limits.max_upload_bytes) : null;
  const rightsIssue = rightsProblem(rights);
  const group = `rights-${character.slug}-${version.version}`;

  const send = async () => {
    if (!file) return;
    const form = new FormData();
    form.append('image_file', file);
    for (const [key, value] of rightsFields(rights)) form.append(key, value);
    const done = await act(
      () => apiFetch(`/api/episodes/${episodeId}/cast/${character.character_id}/versions/${version.version}/images`,
        { method: 'POST', body: form }),
      `Picture of ${character.name} added.`,
    );
    if (done !== null) {
      setFile(null);
      setRights(emptyRights());
      setInputKey((k) => k + 1);
    }
  };

  return (
    <div className="mt-3 p-3 rounded-lg bg-gray-50 dark:bg-gray-900/40" data-testid={`cast-upload-${character.slug}`}>
      <p className="text-sm font-semibold">Add a picture of {character.name}</p>
      <p className="text-xs text-gray-500 mb-2">
        PNG, JPG or WebP, at least {limits.min_image_px} px on each side, up to {megabytes(limits.max_upload_bytes)} MB.
        A clear full-body picture on a plain background works best.
      </p>
      <input
        key={inputKey}
        type="file"
        accept={PICTURE_ACCEPT}
        onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        className="text-sm"
        data-testid={`cast-upload-file-${character.slug}`}
      />
      {fileIssue && <p className="text-xs text-red-700 dark:text-red-300 mt-1">{fileIssue}</p>}
      <fieldset className="mt-3 text-sm space-y-1">
        <legend className="text-xs font-semibold mb-1">Whose picture is this?</legend>
        <label className="flex items-center gap-2">
          <input type="radio" name={group} checked={rights.rights === 'original'}
            onChange={() => setRights({ ...rights, rights: 'original' })} data-testid={`cast-rights-original-${character.slug}`} />
          I made it myself (my own original work)
        </label>
        <label className="flex items-center gap-2">
          <input type="radio" name={group} checked={rights.rights === 'owned'}
            onChange={() => setRights({ ...rights, rights: 'owned' })} data-testid={`cast-rights-owned-${character.slug}`} />
          I own the rights to use it
        </label>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={rights.realPerson}
            onChange={(e) => setRights({ ...rights, realPerson: e.target.checked, likenessConsent: false })}
            data-testid={`cast-real-person-${character.slug}`} />
          It shows a real person
        </label>
        {rights.realPerson && (
          <label className="flex items-center gap-2 ml-6">
            <input type="checkbox" checked={rights.likenessConsent}
              onChange={(e) => setRights({ ...rights, likenessConsent: e.target.checked })}
              data-testid={`cast-likeness-consent-${character.slug}`} />
            That person agreed to their likeness being used in this video
          </label>
        )}
      </fieldset>
      {file && !fileIssue && rightsIssue && (
        <p className="text-xs text-amber-800 dark:text-amber-300 mt-2">{rightsIssue}</p>
      )}
      <button
        type="button"
        onClick={send}
        disabled={busy || !file || !!fileIssue || !!rightsIssue}
        className={`${darkBtn} mt-3`}
        data-testid={`cast-upload-send-${character.slug}`}
      >
        Upload picture
      </button>
    </div>
  );
}

type SheetRequestBody = { stage: 'options' | 'turnaround'; source?: 'upload' | 'text' | 'auto'; count?: number };

function SheetSection({ episodeId, view, character, version, busy, act }: {
  episodeId: string; view: CastView; character: CastCharacter; version: CastVersion; busy: boolean; act: Act;
}) {
  const step = sheetStep(version);
  const pictures = versionPictures(version);
  const hasUpload = pictures.uploads.length > 0;
  const derived = !!version.derived_from_version;
  // null = follow the pictures: from the main picture once there is one, else from the description
  const [chosenSource, setChosenSource] = useState<'upload' | 'text' | null>(null);
  const source = hasUpload ? (chosenSource ?? 'upload') : 'text';
  const fromPicture = derived || source === 'upload';
  const [chosenCount, setChosenCount] = useState<number | null>(null);
  const count = chosenCount !== null && optionCounts(fromPicture).includes(chosenCount)
    ? chosenCount
    : defaultOptionCount(fromPicture);
  const [estimate, setEstimate] = useState<{ request: SheetRequestBody; result: SheetEstimate } | null>(null);
  const [checking, setChecking] = useState(false);
  const [estimateIssue, setEstimateIssue] = useState<string | null>(null);
  const base = `/api/episodes/${episodeId}/cast/${character.character_id}/versions/${version.version}`;
  const problems = requestProblems(version);
  const canEdit = view.can_edit;
  const sheetBlock = view.sheet.model_error
    ? `Character sheets cannot be made on this server right now: ${view.sheet.model_error}`
    : !view.sheet.dry_run && !view.sheet.live_mode
      ? 'Sheet pictures are paid and charged to this video, so switch it to live mode first (Enable Live Mode on this page).'
      : null;

  useEffect(() => {
    setEstimate(null);
    setEstimateIssue(null);
  }, [step, version.version, fromPicture]);

  const checkPrice = async (request: SheetRequestBody) => {
    setChecking(true);
    setEstimate(null);
    setEstimateIssue(null);
    try {
      setEstimate({ request, result: await postJson<SheetEstimate>(`${base}/sheet/estimate`, request) });
    } catch (e) {
      setEstimateIssue(problemText(e));
    } finally {
      setChecking(false);
    }
  };

  const make = async () => {
    if (!estimate) return;
    const started = await act(
      () => postJson(`${base}/sheet/generate`, {
        ...estimate.request,
        approved_total_usd_micros: estimate.result.total_usd_micros,
      }),
      'Started. The pictures show up here when they are ready.',
    );
    if (started !== null) setEstimate(null);
  };

  const reject = (stage: 'options' | 'turnaround' | 'all', question: string) => {
    if (!window.confirm(question)) return;
    act(() => postJson(`${base}/sheet/reject`, { stage }), 'Cleared. You can make new pictures now.');
  };

  const pick = (image: CastImage) => {
    if (pictures.sheet.length > 1 && !window.confirm('Use this look instead? The turnaround made from the old one is cleared.')) return;
    act(() => postJson(`${base}/sheet/pick`, { image_id: image.image_id }), 'Main look picked.');
  };

  const approve = () => {
    if (!window.confirm(`Approve and lock this look of ${character.name}? A locked look cannot be changed; a new outfit becomes a new version.`)) return;
    act(
      () => postJson(`${base}/approve`, { allow_placeholder: view.sheet.dry_run && sheetHasPlaceholders(version) }),
      `${character.name}'s look is locked.`,
    );
  };

  const estimateBox = (label: string) => (
    <div className="mt-2">
      {estimateIssue && <p className="text-sm text-red-700 dark:text-red-300" role="alert">{estimateIssue}</p>}
      {estimate && (
        <div className="p-3 rounded-lg border border-yellow-200 dark:border-yellow-800 bg-yellow-50 dark:bg-yellow-900/20"
          data-testid={`cast-estimate-${character.slug}`}>
          <p className="text-sm font-semibold text-yellow-900 dark:text-yellow-200">{estimateText(estimate.result)}</p>
          {!estimate.result.dry_run && (
            <p className="text-xs text-yellow-800 dark:text-yellow-300 mt-1">
              The studio checks the price, your balance and this video&apos;s budget again before each picture.
            </p>
          )}
          <div className="mt-2 flex gap-2">
            <button type="button" onClick={make} disabled={busy} className={darkBtn}
              data-testid={`cast-generate-${character.slug}`}>
              {label}
            </button>
            <button type="button" onClick={() => setEstimate(null)} disabled={busy} className={plainBtn}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  );

  return (
    <div className="mt-4 border-t border-gray-200 dark:border-gray-700 pt-3" data-testid={`cast-sheet-${character.slug}`}
      data-step={step}>
      <h4 className="text-sm font-semibold">Character sheet</h4>
      <p className="text-sm text-gray-600 dark:text-gray-400 mb-2">{SHEET_STEP_TEXT[step]}</p>

      {problems.length > 0 && (
        <div className="mb-2 p-2 rounded bg-red-50 dark:bg-red-900/20 text-sm text-red-800 dark:text-red-200">
          <p className="font-semibold">Some pictures were not made:</p>
          <ul className="list-disc ml-5">{problems.map((p) => <li key={p}>{p}</li>)}</ul>
        </div>
      )}

      {pictures.options.length > 0 && step !== 'locked' && (
        <div className="mb-3">
          <p className="text-xs font-semibold mb-1">Look options</p>
          <div className="flex flex-wrap gap-3">
            {pictures.options.map((image, i) => (
              <PictureTile key={image.image_id} image={image} label={`Option ${i + 1}`}>
                {canEdit && (
                  <button type="button" onClick={() => pick(image)} disabled={busy || step === 'working'}
                    className={plainBtn} data-testid={`cast-pick-${image.image_id}`}>
                    Use this look
                  </button>
                )}
              </PictureTile>
            ))}
          </div>
        </div>
      )}

      {pictures.sheet.length > 0 && (
        <div className="mb-3">
          <p className="text-xs font-semibold mb-1">{version.locked ? 'Locked sheet' : 'Sheet so far'}</p>
          <div className="flex flex-wrap gap-3">
            {pictures.sheet.map((image) => <PictureTile key={image.image_id} image={image} />)}
          </div>
        </div>
      )}

      {canEdit && step !== 'locked' && step !== 'working' && sheetBlock && (
        <p className="text-sm text-amber-800 dark:text-amber-300">{sheetBlock}</p>
      )}

      {canEdit && !sheetBlock && step === 'start' && (
        <div className="space-y-2">
          {derived ? (
            <p className="text-sm">The options keep the locked look of version {version.derived_from_version} and change the outfit only.</p>
          ) : (
            <fieldset className="text-sm space-y-1">
              <legend className="text-xs font-semibold mb-1">Make the options from</legend>
              <label className="flex items-center gap-2">
                <input type="radio" name={`source-${character.slug}-${version.version}`} checked={source === 'upload'}
                  disabled={!hasUpload} data-testid={`cast-source-upload-${character.slug}`}
                  onChange={() => { setChosenSource('upload'); setChosenCount(null); setEstimate(null); }} />
                {hasUpload ? 'My main picture (it stays the reference for the face and design)' : 'My picture (upload one first)'}
              </label>
              <label className="flex items-center gap-2">
                <input type="radio" name={`source-${character.slug}-${version.version}`} checked={source === 'text'}
                  data-testid={`cast-source-text-${character.slug}`}
                  onChange={() => { setChosenSource('text'); setChosenCount(null); setEstimate(null); }} />
                The description only
              </label>
            </fieldset>
          )}
          <label className="flex items-center gap-2 text-sm">
            How many options
            <select value={count} onChange={(e) => { setChosenCount(Number(e.target.value)); setEstimate(null); }}
              className="px-2 py-1 border border-gray-300 dark:border-gray-700 rounded bg-white dark:bg-gray-900"
              data-testid={`cast-option-count-${character.slug}`}>
              {optionCounts(fromPicture).map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
          <button type="button" disabled={busy || checking}
            onClick={() => checkPrice({ stage: 'options', source: derived ? 'auto' : fromPicture ? 'upload' : 'text', count })}
            className={plainBtn} data-testid={`cast-check-options-${character.slug}`}>
            {checking ? 'Checking the price…' : 'Check the price'}
          </button>
          {estimateBox('Make the options')}
        </div>
      )}

      {canEdit && step === 'pick' && (
        <button type="button" disabled={busy} className={plainBtn}
          onClick={() => reject('options', 'Clear these options and make new ones?')}>
          None of these: make new options
        </button>
      )}

      {canEdit && !sheetBlock && step === 'turnaround' && (
        <div className="space-y-2">
          <button type="button" disabled={busy || checking} onClick={() => checkPrice({ stage: 'turnaround' })}
            className={plainBtn} data-testid={`cast-check-turnaround-${character.slug}`}>
            {checking ? 'Checking the price…' : 'Check the price of the turnaround'}
          </button>
          {estimateBox('Make the turnaround')}
        </div>
      )}

      {canEdit && step === 'approve' && (
        <div className="flex flex-wrap gap-2">
          <button type="button" disabled={busy} onClick={approve} className={`${btn} bg-green-700 text-white`}
            data-testid={`cast-approve-${character.slug}`}>
            Approve and lock this look
          </button>
          <button type="button" disabled={busy} className={plainBtn}
            onClick={() => reject('turnaround', 'Clear the turnaround and make it again from the same main look?')}>
            Redo the turnaround
          </button>
        </div>
      )}

      {canEdit && (step === 'turnaround' || step === 'approve') && (
        <button type="button" disabled={busy} className={`${plainBtn} mt-2`}
          onClick={() => reject('all', 'Clear the whole sheet (options, main look and turnaround) and start over?')}>
          Start the sheet over
        </button>
      )}
    </div>
  );
}

function NewOutfit({ episodeId, character, version, busy, act, onCreated }: {
  episodeId: string; character: CastCharacter; version: CastVersion; busy: boolean; act: Act;
  onCreated: (version: number) => void;
}) {
  const [label, setLabel] = useState('');
  const [outfit, setOutfit] = useState('');
  const create = async () => {
    const made = await act(
      () => postJson<{ version: CastVersion }>(`/api/episodes/${episodeId}/cast/${character.character_id}/versions`, {
        label: label.trim(), outfit: outfit.trim(), derive_from_version: version.version,
      }),
      'New outfit added. Make its sheet below.',
    );
    if (made) {
      setLabel('');
      setOutfit('');
      onCreated(made.version.version);
    }
  };
  return (
    <div className="mt-3 p-3 rounded-lg bg-gray-50 dark:bg-gray-900/40">
      <p className="text-sm font-semibold">New outfit</p>
      <p className="text-xs text-gray-500 mb-2">
        Made from this locked look (never from scene pictures), so the face and body stay the same.
      </p>
      <div className="grid sm:grid-cols-2 gap-2">
        <input value={label} onChange={(e) => setLabel(e.target.value)} maxLength={60} placeholder="Name, e.g. Winter coat"
          className={field} />
        <input value={outfit} onChange={(e) => setOutfit(e.target.value)} maxLength={300}
          placeholder="The outfit, e.g. a long green wool coat and red boots" className={field} />
      </div>
      <button type="button" onClick={create} disabled={busy || !outfit.trim()} className={`${plainBtn} mt-2`}
        data-testid={`cast-new-outfit-${character.slug}`}>
        Add the outfit
      </button>
    </div>
  );
}

function CharacterCard({ episodeId, view, character, busy, act }: {
  episodeId: string; view: CastView; character: CastCharacter; busy: boolean; act: Act;
}) {
  const canEdit = view.can_edit;
  const [versionNo, setVersionNo] = useState(character.default_version);
  const version = character.versions.find((v) => v.version === versionNo) ?? character.versions[0];
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState({ name: character.name, role: character.role, description: character.description });
  const pictures = versionPictures(version);
  const base = `/api/episodes/${episodeId}/cast/${character.character_id}`;
  const anyLocked = character.versions.some((v) => v.locked);

  const save = async () => {
    const saved = await act(() => postJson(base, text), 'Saved.');
    if (saved !== null) setEditing(false);
  };

  const remove = () => {
    if (!window.confirm(`Remove ${character.name} and all their pictures from the cast?`)) return;
    act(() => postJson(`${base}/delete`, { force: false }), `${character.name} removed.`);
  };

  if (!version) return null;
  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-4" data-testid={`cast-character-${character.slug}`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="font-semibold">
            {character.name}
            {character.role && <span className="ml-2 text-sm font-normal text-gray-500">{character.role}</span>}
          </h3>
          <p className="text-xs text-gray-500">Id in the shot plan: <code>{character.slug}</code></p>
        </div>
        {version.locked ? (
          <span className="text-xs px-2 py-1 rounded bg-green-100 dark:bg-green-900/40 text-green-800 dark:text-green-200"
            data-testid={`cast-locked-${character.slug}`}>
            Look locked
          </span>
        ) : (
          <span className="text-xs px-2 py-1 rounded bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300">
            Look not locked yet
          </span>
        )}
      </div>

      {editing ? (
        <div className="mt-3 space-y-2">
          <input value={text.name} onChange={(e) => setText({ ...text, name: e.target.value })} maxLength={60}
            aria-label="Name" className={field} />
          <input value={text.role} onChange={(e) => setText({ ...text, role: e.target.value })} maxLength={80}
            aria-label="Part they play" placeholder="Part they play, e.g. the lead" className={field} />
          <textarea value={text.description} onChange={(e) => setText({ ...text, description: e.target.value })}
            maxLength={600} rows={3} aria-label="What they look like" className={field} />
          {anyLocked && (
            <p className="text-xs text-gray-500">Locked looks keep the description they were locked with.</p>
          )}
          <div className="flex gap-2">
            <button type="button" onClick={save} disabled={busy || !text.name.trim()} className={darkBtn}>Save</button>
            <button type="button" onClick={() => setEditing(false)} disabled={busy} className={plainBtn}>Cancel</button>
          </div>
        </div>
      ) : (
        <p className="text-sm mt-2">
          {character.description || (
            <span className="text-gray-500">No description yet. Say how they look so every shot gets them right.</span>
          )}
        </p>
      )}
      {version.locked && (
        <p className="text-xs text-gray-500 mt-1">Used in every shot with them: &ldquo;{version.clause}&rdquo;</p>
      )}

      {character.versions.length > 1 && (
        <div className="mt-3 flex flex-wrap gap-2" role="group" aria-label="Outfits">
          {character.versions.map((v) => (
            <button key={v.version} type="button" aria-pressed={v.version === version.version}
              onClick={() => setVersionNo(v.version)}
              className={`${btn} border ${v.version === version.version ? 'bg-black text-white dark:bg-white dark:text-black' : 'border-gray-300 dark:border-gray-600'}`}>
              {v.label || `Version ${v.version}`}{v.locked ? ' ✓' : ''}
            </button>
          ))}
        </div>
      )}
      {version.outfit && <p className="text-xs text-gray-600 dark:text-gray-400 mt-2">Outfit: {version.outfit}</p>}

      {pictures.uploads.length > 0 && (
        <div className="mt-3">
          <p className="text-xs font-semibold mb-1">Your pictures</p>
          <div className="flex flex-wrap gap-3">
            {pictures.uploads.map((image) => (
              <PictureTile key={image.image_id} image={image} label={image.is_primary ? 'Main picture' : 'Picture'}>
                {canEdit && !version.locked && !image.is_primary && (
                  <button type="button" disabled={busy} className={plainBtn}
                    onClick={() => act(() => postJson(`${base}/versions/${version.version}/images/${image.image_id}/primary`, {}), 'Main picture changed.')}>
                    Make main
                  </button>
                )}
                {canEdit && !version.locked && (
                  <button type="button" disabled={busy} className={plainBtn}
                    onClick={() => {
                      if (window.confirm('Remove this picture?')) {
                        act(() => postJson(`${base}/versions/${version.version}/images/${image.image_id}/delete`, {}), 'Picture removed.');
                      }
                    }}>
                    Remove
                  </button>
                )}
              </PictureTile>
            ))}
          </div>
        </div>
      )}

      {canEdit && !version.locked && pictures.uploads.length < view.limits.max_uploads_per_version && (
        <PictureUpload episodeId={episodeId} character={character} version={version} view={view} busy={busy} act={act} />
      )}

      <SheetSection key={`${character.character_id}-${version.version}`} episodeId={episodeId} view={view}
        character={character} version={version} busy={busy} act={act} />

      {canEdit && version.locked && (
        <NewOutfit episodeId={episodeId} character={character} version={version} busy={busy} act={act}
          onCreated={setVersionNo} />
      )}

      {canEdit && !editing && (
        <div className="mt-4 flex gap-2">
          <button type="button" onClick={() => {
            setText({ name: character.name, role: character.role, description: character.description });
            setEditing(true);
          }} disabled={busy} className={plainBtn}>
            Edit name and description
          </button>
          <button type="button" onClick={remove} disabled={busy} className={`${plainBtn} text-red-700 dark:text-red-300`}>
            Remove from the cast
          </button>
        </div>
      )}
    </div>
  );
}

function AddCharacter({ episodeId, busy, act }: { episodeId: string; busy: boolean; act: Act }) {
  const empty = { name: '', role: '', description: '' };
  const [form, setForm] = useState(empty);
  const add = async () => {
    const name = form.name.trim();
    const added = await act(
      () => postJson(`/api/episodes/${episodeId}/cast`, {
        name, role: form.role.trim(), description: form.description.trim(),
      }),
      `${name} joined the cast.`,
    );
    if (added !== null) setForm(empty);
  };
  return (
    <div className="mt-4 p-4 rounded-lg border border-dashed border-gray-300 dark:border-gray-600" data-testid="cast-add">
      <h3 className="text-sm font-semibold mb-2">Add someone to the cast</h3>
      <div className="grid sm:grid-cols-2 gap-2">
        <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} maxLength={60}
          placeholder="Name, e.g. Mara" aria-label="Name" className={field} data-testid="cast-add-name" />
        <input value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} maxLength={80}
          placeholder="Part they play, e.g. the lead" aria-label="Part they play" className={field} />
      </div>
      <textarea value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })}
        maxLength={600} rows={3} aria-label="What they look like" data-testid="cast-add-description"
        placeholder="What they look like, the same in every shot: age, build, face, hair, colours, clothing"
        className={`${field} mt-2`} />
      <button type="button" onClick={add} disabled={busy || !form.name.trim()} className={`${darkBtn} mt-2`}
        data-testid="cast-add-button">
        Add to the cast
      </button>
    </div>
  );
}

function SeriesControl({ episodeId, view, busy, act }: { episodeId: string; view: CastView; busy: boolean; act: Act }) {
  const [list, setList] = useState<SeriesInfo[]>([]);
  const [choice, setChoice] = useState('');
  const [newName, setNewName] = useState('');
  const seriesId = view.series?.series_id ?? null;

  useEffect(() => {
    if (!view.can_edit || seriesId) return;
    apiFetch<{ series: SeriesInfo[] }>('/api/series')
      .then((r) => setList(r.series.filter((s) => s.can_edit)))
      .catch(() => setList([]));
  }, [view.can_edit, seriesId]);

  if (view.series) {
    const others = (view.series.episodes ?? []).filter((e) => e !== episodeId).length;
    return (
      <div className="mb-4 text-sm" data-testid="cast-series">
        <p>
          This cast is shared by the series <strong>{view.series.name}</strong>
          {others > 0 ? ` with ${others} other video${others === 1 ? '' : 's'}` : ''}. Changes here show up in every video of the series.
        </p>
        {view.can_edit && (
          <button type="button" disabled={busy} className={`${plainBtn} mt-2`}
            onClick={() => {
              if (window.confirm('Stop sharing the series cast? This video starts with an empty cast; the series keeps everyone.')) {
                act(() => postJson(`/api/episodes/${episodeId}/series`, { series_id: null }), 'This video no longer uses the series cast.');
              }
            }}>
            Stop using the series cast
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="mb-4 text-sm" data-testid="cast-series">
      <p>This cast belongs to this video only. Share it through a series to reuse the same people in later videos.</p>
      {view.can_edit && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {list.length > 0 && (
            <>
              <select value={choice} onChange={(e) => setChoice(e.target.value)} aria-label="Series"
                className="px-2 py-1.5 border border-gray-300 dark:border-gray-700 rounded bg-white dark:bg-gray-900">
                <option value="">Pick a series…</option>
                {list.map((s) => <option key={s.series_id} value={s.series_id}>{s.name}</option>)}
              </select>
              <button type="button" disabled={busy || !choice} className={plainBtn}
                onClick={() => act(() => postJson(`/api/episodes/${episodeId}/series`, { series_id: choice }), 'This video now uses the series cast.')}>
                Use this series
              </button>
              <span className="text-gray-500">or</span>
            </>
          )}
          <input value={newName} onChange={(e) => setNewName(e.target.value)} maxLength={60} placeholder="New series name"
            aria-label="New series name" className="px-2 py-1.5 border border-gray-300 dark:border-gray-700 rounded bg-white dark:bg-gray-900" />
          <button type="button" disabled={busy || !newName.trim()} className={plainBtn}
            onClick={async () => {
              const made = await act(async () => {
                const created = await postJson<{ series: SeriesInfo }>('/api/series', { name: newName.trim() });
                return postJson(`/api/episodes/${episodeId}/series`, { series_id: created.series.series_id });
              }, 'Series started with this cast.');
              if (made !== null) setNewName('');
            }}>
            Start a series with this cast
          </button>
        </div>
      )}
    </div>
  );
}

function ShotCast({ episodeId, view, busy, act }: { episodeId: string; view: CastView; busy: boolean; act: Act }) {
  const [picks, setPicks] = useState<Record<string, CastPick[]>>({});
  const [allowText, setAllowText] = useState<Record<string, boolean | null>>({});
  const shots = view.shots.shots;
  const changes = shotCastChanges(shots, picks, allowText);
  const byslug = Object.fromEntries(view.characters.map((c) => [c.slug, c]));

  const save = async () => {
    if (!changes) return;
    const saved = await act(() => postJson(`/api/episodes/${episodeId}/cast/shots`, changes), 'Saved who is in each shot.');
    if (saved !== null) {
      setPicks({});
      setAllowText({});
    }
  };

  return (
    <div className="mt-6 border-t border-gray-200 dark:border-gray-700 pt-4" data-testid="cast-shots">
      <h3 className="text-base font-semibold mb-1">Who is in each shot</h3>
      <p className="text-sm text-gray-600 dark:text-gray-400 mb-3">
        Pick who appears in each shot. Their description and pictures are added to that shot automatically, and
        pictures never get captions, speech bubbles or lettering unless you allow words for that shot.
      </p>
      {view.shots.error && <p className="text-sm text-red-700 dark:text-red-300">{view.shots.error}</p>}
      {!view.shots.uploaded && <p className="text-sm text-gray-500">Upload this video&apos;s shots first; then choose who is in each one here.</p>}
      {view.shots.plan && view.shots.plan.errors.length > 0 && (
        <div className="mb-3 p-3 rounded bg-red-50 dark:bg-red-900/20 text-sm text-red-800 dark:text-red-200" role="alert"
          data-testid="cast-plan-errors">
          <p className="font-semibold">Fix these before starting:</p>
          <ul className="list-disc ml-5">{view.shots.plan.errors.map((e) => <li key={e}>{e}</li>)}</ul>
        </div>
      )}
      {shots.length > 0 && view.characters.length === 0 && (
        <p className="text-sm text-gray-500">Add someone to the cast first.</p>
      )}
      {shots.length > 0 && view.characters.length > 0 && (
        <ul className="space-y-3">
          {shots.map((shot) => {
            const current = picks[shot.shot_id] ?? castPicks(shot.cast);
            const allowed = (shot.shot_id in allowText ? allowText[shot.shot_id] : shot.allow_text) === true;
            const update = (next: CastPick[]) => setPicks({ ...picks, [shot.shot_id]: next });
            return (
              <li key={shot.shot_id} className="border border-gray-200 dark:border-gray-700 rounded-lg p-3"
                data-testid={`cast-shot-${shot.shot_id}`}>
                <div className="flex flex-wrap items-baseline gap-2">
                  <span className="font-semibold text-sm">{shot.shot_id}</span>
                  <span className="text-xs text-gray-500 line-clamp-2">{shot.prompt}</span>
                </div>
                <div className="mt-2 flex flex-wrap gap-2">
                  {view.characters.map((c) => {
                    const on = current.some((p) => p.character === c.slug);
                    return (
                      <button key={c.slug} type="button" aria-pressed={on} disabled={busy || !view.can_edit}
                        onClick={() => update(toggleCast(current, c.slug))}
                        data-testid={`cast-shot-${shot.shot_id}-${c.slug}`}
                        className={`${btn} border ${on ? 'bg-blue-600 border-blue-600 text-white' : 'border-gray-300 dark:border-gray-600'}`}>
                        {on ? '✓ ' : ''}{c.name}
                      </button>
                    );
                  })}
                </div>
                {current.filter((p) => (byslug[p.character]?.versions.length ?? 0) > 1).map((p) => {
                  const c = byslug[p.character];
                  return (
                    <label key={p.character} className="mt-2 flex items-center gap-2 text-xs">
                      {c.name} wears
                      <select value={p.version ?? ''} disabled={busy || !view.can_edit}
                        onChange={(e) => update(setCastVersion(current, p.character, e.target.value ? Number(e.target.value) : null))}
                        className="px-2 py-1 border border-gray-300 dark:border-gray-700 rounded bg-white dark:bg-gray-900">
                        <option value="">their usual look</option>
                        {c.versions.map((v) => (
                          <option key={v.version} value={v.version}>{v.label || `Version ${v.version}`}{v.locked ? ' (locked)' : ''}</option>
                        ))}
                      </select>
                    </label>
                  );
                })}
                <label className="mt-2 flex items-center gap-2 text-xs">
                  <input type="checkbox" checked={allowed} disabled={busy || !view.can_edit}
                    onChange={(e) => setAllowText({ ...allowText, [shot.shot_id]: e.target.checked ? true : null })} />
                  Words may appear in this picture (a sign or a title card)
                </label>
                {shot.refs.length > 0 && (
                  <p className="mt-2 text-xs text-gray-600 dark:text-gray-400">
                    Reference pictures for this shot: {shot.refs.map((r) => r.label).join(' · ')}
                  </p>
                )}
                {shot.notes.map((note) => (
                  <p key={note} className="mt-1 text-xs text-amber-800 dark:text-amber-300">{note}</p>
                ))}
              </li>
            );
          })}
        </ul>
      )}
      {view.can_edit && shots.length > 0 && view.characters.length > 0 && (
        <button type="button" onClick={save} disabled={busy || !changes} className={`${darkBtn} mt-3`}
          data-testid="cast-shots-save">
          Save who is in each shot
        </button>
      )}
    </div>
  );
}

export default function CastPanel({ episodeId }: { episodeId: string }) {
  const [view, setView] = useState<CastView | null>(null);
  const [loadIssue, setLoadIssue] = useState<string | null>(null);
  const [message, setMessage] = useState<{ tone: 'ok' | 'problem'; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setView(await apiFetch<CastView>(`/api/episodes/${episodeId}/cast`));
      setLoadIssue(null);
    } catch (e) {
      setLoadIssue(e instanceof ApiError ? e.message : 'The cast could not be loaded right now.');
    }
  }, [episodeId]);

  useEffect(() => {
    load();
  }, [load]);

  const working = anyActiveJobs(view?.characters);
  useEffect(() => {
    if (!working) return;
    const timer = setInterval(load, 4000);
    return () => clearInterval(timer);
  }, [working, load]);

  const act: Act = async (fn, done) => {
    setBusy(true);
    setMessage(null);
    try {
      const result = await fn();
      if (done) setMessage({ tone: 'ok', text: done });
      return result;
    } catch (e) {
      setMessage({ tone: 'problem', text: problemText(e) });
      return null;
    } finally {
      setBusy(false);
      await load();
    }
  };

  if (!view) {
    return (
      <div className={card} data-testid="cast-panel">
        <h2 className="text-lg font-semibold mb-1">Cast</h2>
        <p className="text-sm text-gray-600 dark:text-gray-400">{loadIssue ?? 'Loading the cast…'}</p>
      </div>
    );
  }

  return (
    <div className={card} data-testid="cast-panel">
      <h2 className="text-lg font-semibold mb-1">Cast</h2>
      <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
        The people (and creatures) in this video. Describe each one and add a picture, then lock their look with a
        character sheet so they look the same in every shot.
      </p>
      {message && (
        <div role="status" data-testid="cast-message"
          className={`mb-4 p-3 rounded text-sm ${message.tone === 'ok' ? 'bg-green-50 text-green-800 dark:bg-green-900/20 dark:text-green-200' : 'bg-red-50 text-red-800 dark:bg-red-900/20 dark:text-red-200'}`}>
          {message.text}
        </div>
      )}
      {!view.can_edit && (
        <p className="mb-4 text-sm text-gray-600 dark:text-gray-400">
          Only this video&apos;s owner (or an admin) can change its cast.
        </p>
      )}
      <SeriesControl episodeId={episodeId} view={view} busy={busy} act={act} />
      {view.sheet.dry_run && (
        <p className="mb-4 text-xs text-gray-500">
          Preview mode: character sheets use free stand-in pictures, so you can try every step at no cost.
        </p>
      )}
      {view.characters.length === 0 ? (
        <p className="text-sm text-gray-500">No one in the cast yet.</p>
      ) : (
        <div className="space-y-4">
          {view.characters.map((c) => (
            <CharacterCard key={c.character_id} episodeId={episodeId} view={view} character={c} busy={busy} act={act} />
          ))}
        </div>
      )}
      {view.can_edit && view.characters.length < view.limits.max_characters && (
        <AddCharacter episodeId={episodeId} busy={busy} act={act} />
      )}
      <ShotCast episodeId={episodeId} view={view} busy={busy} act={act} />
    </div>
  );
}
