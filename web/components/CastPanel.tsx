'use client';

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { API_URL, ApiError, apiFetch, postJson } from '@/lib/studio-api';
import {
  PICTURE_ACCEPT,
  PICTURE_LABELS,
  SHEET_STEP_TEXT,
  addForm,
  addWithPhotoProblem,
  anyActiveJobs,
  castPicks,
  defaultOptionCount,
  emptyRights,
  estimateText,
  estimateWhat,
  optionCounts,
  pictureHint,
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
  withPictureFields,
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
import { castColours, castColourAt } from '@/lib/castColour.mjs';
import type { CastColour } from '@/lib/castColour.mjs';
import { calmText, moneyProblem, quote, type MoneyProblem } from '@/lib/checkout.mjs';
import { card, focusRing, input as field } from '@/lib/ui';
import Checkout from './Checkout';
import { ConfirmButton } from './ConfirmButton';
import { SizeNote, usePictureCheck } from './PictureSize';
import { RightsQuestions } from './RightsQuestions';
import { CharacterCard as PlayfulCharacterCard } from './playful';

/**
 * The episode's cast: who is in the video, their pictures and locked character sheets, and who is
 * in each shot; or, with `universe`, the characters of a Universe (api/world_routes.py), whose looks
 * are locked from an episode in it. Every action is an API route under `apiBase`
 * (/api/episodes/{id} or /api/worlds/{id}; api/cast_routes.py), which stays the authority: a
 * refused action shows the API's reason (a refused spend in calm words). Paid sheet pictures show
 * their price first, in a checkout (docs/EASY-CHECKOUT.md), and are made only for the amount approved
 * there (the worker checks the estimate, balance and caps again); a price that went up is shown again.
 * The result of an action shows next to it: in the character's card (by the picture upload, the sheet
 * or the outfit when those are on screen), by the add form, the shots or the series.
 */

const btn = 'pf-btn pf-btn-sm';
const plainBtn = `${btn} pf-btn-quiet`;
const darkBtn = `${btn} pf-btn-primary`;
const UNREACHABLE = 'The studio could not be reached. Check your connection and try again.';

/**
 * Where an action's result shows: a character slug (optionally ":upload", ":sheet" or ":outfit"), "add", "shots" or "series".
 * `refused` may take a refusal's words and show them itself (true), instead of the result line.
 */
type Act = <T>(fn: () => Promise<T>, done?: string, where?: string, refused?: (message: string) => boolean) => Promise<T | null>;
type CastNote = { tone: 'ok' | 'problem'; text: string; where: string };

/** The one visible result line (data-testid="cast-message"); the panel's live region announces it. */
function CastMessage({ note }: { note: CastNote | null | undefined }) {
  if (!note) return null;
  return (
    <p data-testid="cast-message" data-tone={note.tone}
      className={`my-3 rounded-inner p-3 text-[13px] font-bold ${note.tone === 'ok' ? 'bg-green-soft text-green-ink' : 'bg-coral-soft text-coral-ink'}`}>
      {note.text}
    </p>
  );
}

function problemText(e: unknown): string {
  return e instanceof ApiError ? calmText(e.message) : UNREACHABLE;
}

function Thumb({ image, label }: { image: CastImage; label?: string }) {
  const text = label ?? PICTURE_LABELS[image.role] ?? image.role;
  return (
    <a href={`${API_URL}${image.url}`} target="_blank" rel="noreferrer" className={`block rounded ${focusRing}`} title="Open the full picture">
      <img
        src={`${API_URL}${image.thumb_url}`}
        alt={text}
        loading="lazy"
        className="h-36 w-36 rounded-[12px] bg-mist object-cover shadow-card"
      />
    </a>
  );
}

function PictureTile({ image, label, children }: { image: CastImage; label?: string; children?: ReactNode }) {
  return (
    <figure className="w-36" data-testid={`cast-image-${image.image_id}`}>
      <Thumb image={image} label={label} />
      <figcaption className="mt-1 text-[12px] font-bold text-ink-soft">
        {label ?? PICTURE_LABELS[image.role] ?? image.role}
        {image.placeholder && <span className="block text-mustard-ink">stand-in (preview)</span>}
      </figcaption>
      <SizeNote text={image.size_note} testId={`cast-image-size-${image.image_id}`} />
      {children && <div className="mt-1 flex flex-col gap-1">{children}</div>}
    </figure>
  );
}

/** A character sheet as a compact grid of thumbnails, each opening the full picture. */
function SheetGrid({ images }: { images: CastImage[] }) {
  return (
    <>
      <ul className="grid grid-cols-4 gap-1.5">
        {images.map((image) => {
          const text = PICTURE_LABELS[image.role] ?? image.role;
          return (
            <li key={image.image_id} data-testid={`cast-image-${image.image_id}`}>
              <a href={`${API_URL}${image.url}`} target="_blank" rel="noreferrer" title={`${text}: open the full picture`}
                className={`block rounded-[10px] ${focusRing}`}>
                <img src={`${API_URL}${image.thumb_url}`} alt={text} loading="lazy"
                  className="aspect-square w-full rounded-[10px] bg-mist object-cover shadow-card" />
              </a>
            </li>
          );
        })}
      </ul>
      {images.some((image) => image.placeholder) && (
        <p className="mt-1 text-[11.5px] font-bold text-mustard-ink">Stand-in pictures (preview)</p>
      )}
    </>
  );
}

function PictureUpload({ apiBase, character, version, view, busy, act, note }: {
  apiBase: string; character: CastCharacter; version: CastVersion; view: CastView; busy: boolean; act: Act;
  note: CastNote | null;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [rights, setRights] = useState<RightsAnswers>(emptyRights());
  const [inputKey, setInputKey] = useState(0);
  const limits = view.limits;
  const size = usePictureCheck(file, limits);
  const fileIssue = file ? (pictureProblem(file, limits.max_upload_bytes) ?? size.problem) : null;
  const rightsIssue = rightsProblem(rights);
  const group = `rights-${character.slug}-${version.version}`;

  const send = async () => {
    if (!file) return;
    const form = new FormData();
    form.append('image_file', file);
    for (const [key, value] of rightsFields(rights)) form.append(key, value);
    const done = await act(
      () => apiFetch(`${apiBase}/cast/${character.character_id}/versions/${version.version}/images`,
        { method: 'POST', body: form }),
      `Picture of ${character.name} added.`,
      `${character.slug}:upload`,
    );
    if (done !== null) {
      setFile(null);
      setRights(emptyRights());
      setInputKey((k) => k + 1);
    }
  };

  return (
    <div className="mt-3 p-3 rounded-lg bg-gray-50" data-testid={`cast-upload-${character.slug}`}>
      <p className="text-sm font-semibold">Add a picture of {character.name}</p>
      <p className="text-xs text-gray-600 mb-2" id={`cast-upload-hint-${character.slug}`}>
        {pictureHint(limits)} A clear full-body picture on a plain background works best.
      </p>
      <input
        key={inputKey}
        type="file"
        accept={PICTURE_ACCEPT}
        onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        className={`max-w-full text-sm rounded ${focusRing}`}
        aria-label={`Picture of ${character.name}`}
        aria-describedby={`cast-upload-hint-${character.slug}`}
        data-testid={`cast-upload-file-${character.slug}`}
      />
      {fileIssue && <p className="text-xs text-red-700 mt-1">{fileIssue}</p>}
      {!fileIssue && <SizeNote text={size.note} testId={`cast-upload-size-${character.slug}`} className="mt-1" />}
      <RightsQuestions rights={rights} onChange={setRights} group={group} testId={(what) => `cast-${what}-${character.slug}`} />
      {file && !fileIssue && rightsIssue && (
        <p className="text-xs text-amber-800 mt-2">{rightsIssue}</p>
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
      <CastMessage note={note} />
    </div>
  );
}

type SheetRequestBody = { stage: 'options' | 'turnaround'; source?: 'upload' | 'text' | 'auto'; count?: number };

function SheetSection({ apiBase, view, character, version, busy, act, note }: {
  apiBase: string; view: CastView; character: CastCharacter; version: CastVersion; busy: boolean; act: Act;
  note: CastNote | null;
}) {
  const here = `${character.slug}:sheet`;
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
  const [refusal, setRefusal] = useState<MoneyProblem | null>(null);
  const base = `${apiBase}/cast/${character.character_id}/versions/${version.version}`;
  const problems = requestProblems(version);
  const canEdit = view.can_edit;
  const sheetBlock = view.sheet.model_error
    ? `Character sheets cannot be made on this server right now: ${view.sheet.model_error}`
    : !view.sheet.dry_run && !view.sheet.live_mode
      ? 'Sheet pictures are paid and charged to this video, so switch it to live mode first (Production details, Live mode, at the bottom of this page).'
      : null;

  useEffect(() => {
    setEstimate(null);
    setEstimateIssue(null);
    setRefusal(null);
  }, [step, version.version, fromPicture]);

  const checkPrice = async (request: SheetRequestBody) => {
    setChecking(true);
    setEstimate(null);
    setEstimateIssue(null);
    setRefusal(null);
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
    setRefusal(null);
    const started = await act(
      () => postJson(`${base}/sheet/generate`, {
        ...estimate.request,
        approved_total_usd_micros: estimate.result.total_usd_micros,
      }),
      'Started. The pictures show up here when they are ready.',
      here,
      (message) => {
        const money = moneyProblem(message);
        setRefusal(money);
        return money !== null;
      },
    );
    if (started !== null) setEstimate(null);
  };

  const reject = (stage: 'options' | 'turnaround' | 'all') =>
    act(() => postJson(`${base}/sheet/reject`, { stage }), 'Cleared. You can make new pictures now.', here);

  const pick = (image: CastImage) =>
    act(() => postJson(`${base}/sheet/pick`, { image_id: image.image_id }), 'Main look picked.', here);

  // Locking is permanent, so it keeps the browser's own confirmation dialog.
  const approve = () => {
    if (!window.confirm(`Approve and lock this look of ${character.name}? A locked look cannot be changed; a new outfit becomes a new version.`)) return;
    act(
      () => postJson(`${base}/approve`, { allow_placeholder: view.sheet.dry_run && sheetHasPlaceholders(version) }),
      `${character.name}'s look is locked.`,
      here,
    );
  };

  const estimateBox = (label: string) => (
    <div className="mt-2">
      {estimateIssue && <p className="text-sm text-red-700" role="alert">{estimateIssue}</p>}
      {estimate && (
        <Checkout testId={`cast-estimate-${character.slug}`} what={estimateWhat(estimate.result)}
          title={estimateText(estimate.result)} price={quote(estimate.result)} action={label} onAction={make} busy={busy}
          actionTestId={`cast-generate-${character.slug}`} notNow={() => setEstimate(null)}
          notNowTestId={`cast-not-now-${character.slug}`} problem={refusal} onProblem={() => checkPrice(estimate.request)} />
      )}
    </div>
  );

  return (
    <div className="mt-4 border-t border-gray-200 pt-3" data-testid={`cast-sheet-${character.slug}`}
      data-step={step}>
      <h4 className="text-sm font-semibold">Character sheet</h4>
      <p className="text-sm text-gray-600 mb-2">{SHEET_STEP_TEXT[step]}</p>

      {problems.length > 0 && (
        <div className="mb-2 p-2 rounded bg-red-50 text-sm text-red-800">
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
                  <ConfirmButton testId={`cast-pick-${image.image_id}`} disabled={busy || step === 'working'} className={plainBtn}
                    question={pictures.sheet.length > 1 ? 'Use this look instead? The turnaround made from the old one is cleared.' : null}
                    confirmLabel="Yes, use it" onConfirm={() => pick(image)}>
                    Use this look
                  </ConfirmButton>
                )}
              </PictureTile>
            ))}
          </div>
        </div>
      )}

      {pictures.sheet.length > 0 && (
        <div className="mb-3">
          <p className="text-xs font-semibold mb-1">{version.locked ? 'Locked sheet' : 'Sheet so far'}</p>
          <SheetGrid images={pictures.sheet} />
        </div>
      )}

      {canEdit && step !== 'locked' && step !== 'working' && sheetBlock && (
        <p className="text-sm text-amber-800">{sheetBlock}</p>
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
              className="px-2 py-1 border border-gray-300 rounded bg-white"
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
        <ConfirmButton disabled={busy} className={plainBtn} question="Clear these options and make new ones?"
          confirmLabel="Clear them" onConfirm={() => reject('options')}>
          None of these: make new options
        </ConfirmButton>
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
          <ConfirmButton disabled={busy} className={plainBtn} confirmLabel="Clear it"
            question="Clear the turnaround and make it again from the same main look?" onConfirm={() => reject('turnaround')}>
            Redo the turnaround
          </ConfirmButton>
        </div>
      )}

      {canEdit && (step === 'turnaround' || step === 'approve') && (
        <div className="mt-2">
          <ConfirmButton disabled={busy} className={plainBtn} confirmLabel="Start over"
            question="Clear the whole sheet (options, main look and turnaround) and start over?" onConfirm={() => reject('all')}>
            Start the sheet over
          </ConfirmButton>
        </div>
      )}
      <CastMessage note={note} />
    </div>
  );
}

function NewOutfit({ apiBase, character, version, busy, act, onCreated, note }: {
  apiBase: string; character: CastCharacter; version: CastVersion; busy: boolean; act: Act;
  onCreated: (version: number) => void; note: CastNote | null;
}) {
  const [label, setLabel] = useState('');
  const [outfit, setOutfit] = useState('');
  const create = async () => {
    const made = await act(
      () => postJson<{ version: CastVersion }>(`${apiBase}/cast/${character.character_id}/versions`, {
        label: label.trim(), outfit: outfit.trim(), derive_from_version: version.version,
      }),
      'New outfit added. Make its sheet below.',
      `${character.slug}:outfit`,
    );
    if (made) {
      setLabel('');
      setOutfit('');
      onCreated(made.version.version);
    }
  };
  return (
    <div className="mt-3 p-3 rounded-lg bg-gray-50">
      <p className="text-sm font-semibold">New outfit</p>
      <p className="text-xs text-gray-600 mb-2">
        Made from this locked look (never from scene pictures), so the face and body stay the same.
      </p>
      <div className="grid sm:grid-cols-2 gap-2">
        <input value={label} onChange={(e) => setLabel(e.target.value)} maxLength={60} placeholder="Name, e.g. Winter coat"
          aria-label="Outfit name" className={field} />
        <input value={outfit} onChange={(e) => setOutfit(e.target.value)} maxLength={300}
          placeholder="The outfit, e.g. a long green wool coat and red boots" aria-label="The outfit" className={field} />
      </div>
      <button type="button" onClick={create} disabled={busy || !outfit.trim()} className={`${plainBtn} mt-2`}
        data-testid={`cast-new-outfit-${character.slug}`}>
        Add the outfit
      </button>
      <CastMessage note={note} />
    </div>
  );
}

function CharacterCard({ apiBase, view, character, colour, index, busy, act, note, universe, badge, extra }: {
  apiBase: string; view: CastView; character: CastCharacter; colour: CastColour; index: number; busy: boolean; act: Act;
  note: CastNote | null; universe: UniverseCast | null; badge?: CastPanelProps['badge']; extra?: CastPanelProps['extra'];
}) {
  const canEdit = view.can_edit;
  const [versionNo, setVersionNo] = useState(character.default_version);
  const version = character.versions.find((v) => v.version === versionNo) ?? character.versions[0];
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState({ name: character.name, role: character.role, description: character.description });
  const pictures = versionPictures(version);
  const base = `${apiBase}/cast/${character.character_id}`;
  const anyLocked = character.versions.some((v) => v.locked);
  const here = character.slug;

  const save = async () => {
    const saved = await act(() => postJson(base, text), 'Saved.', here);
    if (saved !== null) setEditing(false);
  };

  const remove = () => act(() => postJson(`${base}/delete`, { force: false }), `${character.name} removed.`, here);

  if (!version) return null;
  const uploadShown = canEdit && !version.locked && pictures.uploads.length < view.limits.max_uploads_per_version;
  const outfitShown = canEdit && version.locked && !universe;
  // A result shows by the part of the card it came from while that part is on screen, else at the card's foot.
  const spot = note?.where.startsWith(`${here}:`) ? note.where.slice(here.length + 1) : null;
  const inPlace = (spot === 'upload' && uploadShown) || spot === 'sheet' || (spot === 'outfit' && outfitShown);
  const noteFor = (part: string) => (inPlace && spot === part ? note : null);
  const cardNote = note && (note.where === here || (spot !== null && !inPlace)) ? note : null;
  const face = pictures.uploads.find((image) => image.is_primary) ?? pictures.uploads[0] ?? pictures.sheet[0] ?? null;
  return (
    <PlayfulCharacterCard testId={`cast-character-${character.slug}`} colour={colour} name={character.name}
      picture={face ? { src: `${API_URL}${face.thumb_url}`, alt: '' } : null}
      needsYou={canEdit && !anyLocked}
      title={(
        <>
          <h3 className="break-words text-[19px]">{character.name}</h3>
          {character.role && <p className="text-[12.5px] font-bold text-ink-soft">{character.role}</p>}
        </>
      )}>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        {badge ? badge(character, version) : version.locked ? (
          <span className="pf-chip pf-chip-lock" data-testid={`cast-locked-${character.slug}`}>
            Look locked
          </span>
        ) : (
          <span className="pf-chip pf-chip-open">
            Look not locked yet
          </span>
        )}
        <span className="text-[11.5px] text-ink-soft">Id in the shot plan: <code>{character.slug}</code></span>
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
            <p className="text-[12px] text-ink-soft">Locked looks keep the description they were locked with.</p>
          )}
          <div className="flex gap-2">
            <button type="button" onClick={save} disabled={busy || !text.name.trim()} className={darkBtn}>Save</button>
            <button type="button" onClick={() => setEditing(false)} disabled={busy} className={plainBtn}>Cancel</button>
          </div>
        </div>
      ) : (
        <p className="mt-2 text-[13.5px]">
          {character.description || (
            <span className="text-ink-soft">No description yet. Say how they look so every shot gets them right.</span>
          )}
        </p>
      )}
      {version.locked && (
        <p className="mt-1 text-[12px] text-ink-soft">Used in every shot with them: &ldquo;{version.clause}&rdquo;</p>
      )}
      {extra?.(character)}

      {character.versions.length > 1 && (
        <div className="mt-3 flex flex-wrap gap-2" role="group" aria-label="Outfits">
          {character.versions.map((v) => (
            <button key={v.version} type="button" aria-pressed={v.version === version.version}
              onClick={() => setVersionNo(v.version)}
              className={`pf-chip min-h-9 cursor-pointer ${v.version === version.version ? 'bg-green-deep text-white' : 'bg-mist text-ink-soft'}`}>
              {v.label || `Version ${v.version}`}{v.locked ? ' · locked' : ''}
            </button>
          ))}
        </div>
      )}
      {version.outfit && <p className="mt-2 text-[12px] text-ink-soft">Outfit: {version.outfit}</p>}

      {pictures.uploads.length > 0 && (
        <div className="mt-3">
          <p className="mb-1 text-[12px] font-extrabold">Your pictures</p>
          <div className="flex flex-wrap gap-3">
            {pictures.uploads.map((image) => (
              <PictureTile key={image.image_id} image={image} label={image.is_primary ? 'Main picture' : 'Picture'}>
                {canEdit && !version.locked && !image.is_primary && (
                  <button type="button" disabled={busy} className={plainBtn}
                    onClick={() => act(() => postJson(`${base}/versions/${version.version}/images/${image.image_id}/primary`, {}), 'Main picture changed.', here)}>
                    Make main
                  </button>
                )}
                {canEdit && !version.locked && (
                  <ConfirmButton disabled={busy} className={plainBtn} question="Remove this picture?" confirmLabel="Remove it"
                    onConfirm={() => act(() => postJson(`${base}/versions/${version.version}/images/${image.image_id}/delete`, {}), 'Picture removed.', here)}>
                    Remove
                  </ConfirmButton>
                )}
              </PictureTile>
            ))}
          </div>
        </div>
      )}

      {uploadShown && (
        <PictureUpload apiBase={apiBase} character={character} version={version} view={view} busy={busy} act={act}
          note={noteFor('upload')} />
      )}

      {universe ? (
        <UniverseLook character={character} version={version} lockEpisode={universe.lockEpisode} canEdit={canEdit} />
      ) : (
        <SheetSection key={`${character.character_id}-${version.version}`} apiBase={apiBase} view={view}
          character={character} version={version} busy={busy} act={act} note={noteFor('sheet')} />
      )}

      {outfitShown && (
        <NewOutfit apiBase={apiBase} character={character} version={version} busy={busy} act={act}
          onCreated={setVersionNo} note={noteFor('outfit')} />
      )}

      {canEdit && !editing && (
        <div className="mt-4 flex flex-wrap gap-2">
          <button type="button" onClick={() => {
            setText({ name: character.name, role: character.role, description: character.description });
            setEditing(true);
          }} disabled={busy} className={plainBtn}>
            Edit name and description
          </button>
          <ConfirmButton disabled={busy} className={`${plainBtn} !text-coral-ink`} confirmLabel="Remove"
            question={`Remove ${character.name} and all their pictures from ${universe ? 'this Universe' : 'the cast'}?`}
            onConfirm={remove}>
            {universe ? 'Remove from this Universe' : 'Remove from the cast'}
          </ConfirmButton>
        </div>
      )}
      <CastMessage note={cardNote} />
    </PlayfulCharacterCard>
  );
}

/**
 * Add someone to the cast: name, part, description and a photo in one step (the photo with the same
 * checks and rights questions as a picture upload), or without a photo. With no one in the cast yet
 * the photo comes first and "Add a character with a photo" is the main action.
 */
function AddCharacter({ apiBase, view, busy, act, note }: {
  apiBase: string; view: CastView; busy: boolean; act: Act; note: CastNote | null;
}) {
  const empty = { name: '', role: '', description: '' };
  const [form, setForm] = useState(empty);
  const [file, setFile] = useState<File | null>(null);
  const [rights, setRights] = useState<RightsAnswers>(emptyRights());
  const [inputKey, setInputKey] = useState(0);
  const { first, title } = addForm(view);
  const limits = view.limits;
  const name = form.name.trim();
  const size = usePictureCheck(file, limits);
  const fileIssue = file ? (pictureProblem(file, limits.max_upload_bytes) ?? size.problem) : null;
  const photoIssue = addWithPhotoProblem(name, file, rights, limits.max_upload_bytes) ?? size.problem;

  const added = (result: unknown) => {
    if (result === null) return;
    setForm(empty);
    setFile(null);
    setRights(emptyRights());
    setInputKey((k) => k + 1);
  };
  const addWithPhoto = async () => {
    if (!file || photoIssue) return;
    const body = new FormData();
    body.append('image_file', file);
    for (const [key, value] of withPictureFields(form, rights)) body.append(key, value);
    added(await act(() => apiFetch(`${apiBase}/cast/with-picture`, { method: 'POST', body }),
      `${name} joined the cast with their photo.`, 'add'));
  };
  const addWithoutPhoto = async () => {
    added(await act(() => postJson(`${apiBase}/cast`, {
      name, role: form.role.trim(), description: form.description.trim(),
    }), `${name} joined the cast.`, 'add'));
  };
  return (
    <div data-testid="cast-add" data-first={first ? 'true' : 'false'}
      className={`mt-4 p-4 rounded-lg border ${first ? 'border-blue-300 bg-blue-50/40' : 'border-dashed border-gray-300'}`}>
      <h3 className="text-sm font-semibold mb-1" data-testid="cast-add-title">{title}</h3>
      {first && (
        <p className="text-sm text-gray-700 mb-3">
          Start with a photo or drawing of each character: the studio makes their character sheet from it, so they
          look the same in every shot.
        </p>
      )}
      <label htmlFor="cast-add-file" className="block text-sm font-medium">{first ? 'Photo' : 'Photo (optional)'}</label>
      <p id="cast-add-file-hint" className="text-xs text-gray-600 mb-1">
        {pictureHint(limits)} A clear full-body picture on a plain background works best.
      </p>
      <input id="cast-add-file" key={inputKey} type="file" accept={PICTURE_ACCEPT}
        onChange={(e) => setFile(e.target.files?.[0] ?? null)} aria-describedby="cast-add-file-hint"
        className={`max-w-full text-sm rounded ${focusRing}`} data-testid="cast-add-file" />
      {fileIssue && <p className="text-xs text-red-700 mt-1">{fileIssue}</p>}
      {!fileIssue && <SizeNote text={size.note} testId="cast-add-size-note" className="mt-1" />}
      <div className="grid sm:grid-cols-2 gap-2 mt-3">
        <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} maxLength={60}
          placeholder="Name, e.g. Mara" aria-label="Name" className={field} data-testid="cast-add-name" />
        <input value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} maxLength={80}
          placeholder="Part they play, e.g. the lead" aria-label="Part they play" className={field}
          data-testid="cast-add-role" />
      </div>
      <textarea value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })}
        maxLength={600} rows={3} aria-label="What they look like" data-testid="cast-add-description"
        placeholder="What they look like, the same in every shot: age, build, face, hair, colours, clothing"
        className={`${field} mt-2`} />
      {file && (
        <RightsQuestions rights={rights} onChange={setRights} group="rights-add" testId={(what) => `cast-add-${what}`} />
      )}
      {file && !fileIssue && photoIssue && (
        <p className="text-xs text-amber-800 mt-2" data-testid="cast-add-photo-problem">{photoIssue}</p>
      )}
      <div className="mt-3 flex flex-wrap gap-2">
        <button type="button" onClick={addWithPhoto} disabled={busy || !!photoIssue} className={darkBtn}
          data-testid="cast-add-with-photo">
          Add a character with a photo
        </button>
        <button type="button" onClick={addWithoutPhoto} disabled={busy || !name} className={plainBtn}
          data-testid="cast-add-button">
          Add without a photo
        </button>
      </div>
      <CastMessage note={note} />
    </div>
  );
}

function SeriesControl({ episodeId, apiBase, view, busy, act, note }: {
  episodeId: string; apiBase: string; view: CastView; busy: boolean; act: Act; note: CastNote | null;
}) {
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
          This cast belongs to the Universe <strong>{view.series.name}</strong>
          {others > 0 ? ` and is shared with ${others} other video${others === 1 ? '' : 's'}` : ''}. Changes here show up in every video of the Universe.
        </p>
        {view.can_edit && (
          <div className="mt-2">
            <ConfirmButton disabled={busy} className={plainBtn} confirmLabel="Take it out"
              question="Take this video out of the Universe? It starts with an empty cast; the Universe keeps everyone."
              onConfirm={() => act(() => postJson(`${apiBase}/series`, { series_id: null }),
                'This video is no longer in the Universe.', 'series')}>
              Take this video out of the Universe
            </ConfirmButton>
          </div>
        )}
        <CastMessage note={note} />
      </div>
    );
  }

  return (
    <div className="mb-4 text-sm" data-testid="cast-series">
      <p>This cast belongs to this video only. Put the video in a Universe to reuse the same people in later videos.</p>
      {view.can_edit && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {list.length > 0 && (
            <>
              <select value={choice} onChange={(e) => setChoice(e.target.value)} aria-label="Universe"
                className="px-2 py-1.5 border border-gray-300 rounded bg-white">
                <option value="">Pick a Universe…</option>
                {list.map((s) => <option key={s.series_id} value={s.series_id}>{s.name}</option>)}
              </select>
              <button type="button" disabled={busy || !choice} className={plainBtn}
                onClick={() => act(() => postJson(`${apiBase}/series`, { series_id: choice }), 'This video is now in the Universe.', 'series')}>
                Use this Universe
              </button>
              <span className="text-gray-600">or</span>
            </>
          )}
          <input value={newName} onChange={(e) => setNewName(e.target.value)} maxLength={60} placeholder="New Universe name"
            aria-label="New Universe name" className="px-2 py-1.5 border border-gray-300 rounded bg-white" />
          <button type="button" disabled={busy || !newName.trim()} className={plainBtn}
            onClick={async () => {
              const made = await act(async () => {
                const created = await postJson<{ series: SeriesInfo }>('/api/series', { name: newName.trim() });
                return postJson(`${apiBase}/series`, { series_id: created.series.series_id });
              }, 'Universe started with this cast.', 'series');
              if (made !== null) setNewName('');
            }}>
            Start a Universe with this cast
          </button>
        </div>
      )}
      <CastMessage note={note} />
    </div>
  );
}

function ShotCast({ apiBase, view, busy, act, note }: {
  apiBase: string; view: CastView; busy: boolean; act: Act; note: CastNote | null;
}) {
  const [picks, setPicks] = useState<Record<string, CastPick[]>>({});
  const [allowText, setAllowText] = useState<Record<string, boolean | null>>({});
  const shots = view.shots.shots;
  const changes = shotCastChanges(shots, picks, allowText);
  const byslug = Object.fromEntries(view.characters.map((c) => [c.slug, c]));

  const save = async () => {
    if (!changes) return;
    const saved = await act(() => postJson(`${apiBase}/cast/shots`, changes), 'Saved who is in each shot.', 'shots');
    if (saved !== null) {
      setPicks({});
      setAllowText({});
    }
  };

  return (
    <div className="mt-6 border-t border-gray-200 pt-4" data-testid="cast-shots">
      <h3 className="text-base font-semibold mb-1">Who is in each shot</h3>
      <p className="text-sm text-gray-600 mb-3">
        Pick who appears in each shot. Their description and pictures are added to that shot automatically, and
        pictures never get captions, speech bubbles or lettering unless you allow words for that shot.
      </p>
      {view.shots.error && <p className="text-sm text-red-700">{view.shots.error}</p>}
      {!view.shots.uploaded && <p className="text-sm text-gray-600">Upload this video&apos;s shots first; then choose who is in each one here.</p>}
      {view.shots.plan && view.shots.plan.errors.length > 0 && (
        <div className="mb-3 p-3 rounded bg-red-50 text-sm text-red-800" role="alert"
          data-testid="cast-plan-errors">
          <p className="font-semibold">Fix these before starting:</p>
          <ul className="list-disc ml-5">{view.shots.plan.errors.map((e) => <li key={e}>{e}</li>)}</ul>
        </div>
      )}
      {shots.length > 0 && view.characters.length === 0 && (
        <p className="text-sm text-gray-600">Add someone to the cast first.</p>
      )}
      {shots.length > 0 && view.characters.length > 0 && (
        <ul className="space-y-3">
          {shots.map((shot) => {
            const current = picks[shot.shot_id] ?? castPicks(shot.cast);
            const allowed = (shot.shot_id in allowText ? allowText[shot.shot_id] : shot.allow_text) === true;
            const update = (next: CastPick[]) => setPicks({ ...picks, [shot.shot_id]: next });
            return (
              <li key={shot.shot_id} className="border border-gray-200 rounded-lg p-3"
                data-testid={`cast-shot-${shot.shot_id}`}>
                <div className="flex flex-wrap items-baseline gap-2">
                  <span className="font-semibold text-sm">{shot.shot_id}</span>
                  <span className="text-xs text-gray-600 line-clamp-2">{shot.prompt}</span>
                </div>
                <div className="mt-2 flex flex-wrap gap-2">
                  {view.characters.map((c) => {
                    const on = current.some((p) => p.character === c.slug);
                    return (
                      <button key={c.slug} type="button" aria-pressed={on} disabled={busy || !view.can_edit}
                        onClick={() => update(toggleCast(current, c.slug))}
                        data-testid={`cast-shot-${shot.shot_id}-${c.slug}`}
                        className={`${btn} border ${on ? 'bg-blue-600 border-blue-600 text-white' : 'border-gray-300'}`}>
                        {c.name}
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
                        className="px-2 py-1 border border-gray-300 rounded bg-white">
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
                  <p className="mt-2 text-xs text-gray-600">
                    Reference pictures for this shot: {shot.refs.map((r) => r.label).join(' · ')}
                  </p>
                )}
                {shot.notes.map((note) => (
                  <p key={note} className="mt-1 text-xs text-amber-800">{note}</p>
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
      <CastMessage note={note} />
    </div>
  );
}

/**
 * The Universe page's characters: its view of GET /api/worlds/{id} as a cast, how to reload it after a
 * change, and the open episode of the Universe where a look is locked (null: none yet).
 */
export interface UniverseCast {
  view: CastView;
  reload: () => Promise<void>;
  lockEpisode: string | null;
}

interface CastPanelProps {
  episodeId?: string;
  /** /api/episodes/{id} (the default) or /api/worlds/{id}. */
  apiBase?: string;
  universe?: UniverseCast | null;
  /** In place of a character's lock chip. */
  badge?: (character: CastCharacter, version: CastVersion) => ReactNode;
  /** More about a character, under its description. */
  extra?: (character: CastCharacter) => ReactNode;
  onView?: (view: CastView) => void;
}

/** A Universe character's look: a sheet is made and locked from an episode in the Universe. */
function UniverseLook({ character, version, lockEpisode, canEdit }: {
  character: CastCharacter; version: CastVersion; lockEpisode: string | null; canEdit: boolean;
}) {
  const sheet = versionPictures(version).sheet;
  return (
    <div className="mt-4 border-t border-gray-200 pt-3" data-testid={`cast-sheet-${character.slug}`}>
      <h4 className="text-sm font-semibold">Character sheet</h4>
      {sheet.length > 0 && (
        <div className="my-2">
          <p className="text-xs font-semibold mb-1">{version.locked ? 'Locked sheet' : 'Sheet so far'}</p>
          <SheetGrid images={sheet} />
        </div>
      )}
      {canEdit && !version.locked && (
        <p className="text-sm text-gray-700" data-testid={`universe-lock-${character.slug}`}>
          Lock this look from an episode in this Universe
          {lockEpisode ? (
            <>
              {': '}
              <a href={`/studio/episodes/${encodeURIComponent(lockEpisode)}#cast`}
                className={`font-semibold text-blue-700 underline-offset-2 hover:underline rounded ${focusRing}`}
                data-testid={`universe-lock-link-${character.slug}`}>
                open {lockEpisode}
              </a>.
            </>
          ) : '. Start a new episode in this Universe, then make and lock the sheet on its page.'}
        </p>
      )}
    </div>
  );
}

/** onView: every loaded cast, for the page's Next step (characters in the shot plan without a locked look). */
export default function CastPanel({ episodeId = '', apiBase, universe = null, badge, extra, onView }: CastPanelProps) {
  const base = apiBase ?? `/api/episodes/${episodeId}`;
  const [loaded, setView] = useState<CastView | null>(null);
  const view = universe ? universe.view : loaded;
  const [loadIssue, setLoadIssue] = useState<string | null>(null);
  const [message, setMessage] = useState<CastNote | null>(null);
  const [busy, setBusy] = useState(false);
  const onViewRef = useRef(onView);
  useEffect(() => {
    onViewRef.current = onView;
  }, [onView]);

  const reloadUniverse = universe?.reload;
  const load = useCallback(async () => {
    if (reloadUniverse) {
      await reloadUniverse();
      return;
    }
    try {
      const cast = await apiFetch<CastView>(`${base}/cast`);
      setView(cast);
      onViewRef.current?.(cast);
      setLoadIssue(null);
    } catch (e) {
      setLoadIssue(e instanceof ApiError ? e.message : 'The cast could not be loaded right now.');
    }
  }, [base, reloadUniverse]);

  const inUniverse = Boolean(universe);
  useEffect(() => {
    if (!inUniverse) load();
  }, [load, inUniverse]);

  const working = !inUniverse && anyActiveJobs(view?.characters);
  useEffect(() => {
    if (!working) return;
    const timer = setInterval(load, 4000);
    return () => clearInterval(timer);
  }, [working, load]);

  const act: Act = async (fn, done, where = 'panel', refused) => {
    setBusy(true);
    setMessage(null);
    try {
      const result = await fn();
      if (done) setMessage({ tone: 'ok', text: done, where });
      return result;
    } catch (e) {
      if (!(e instanceof ApiError && refused?.(e.message))) setMessage({ tone: 'problem', text: problemText(e), where });
      return null;
    } finally {
      setBusy(false);
      await load();
    }
  };

  if (!view) {
    return (
      <div className={universe ? 'min-w-0' : card} data-testid="cast-panel">
        <h2 className="mb-1 text-[24px]">{universe ? 'Characters' : 'Cast'}</h2>
        <p className="text-[13.5px] text-ink-soft">{loadIssue ?? 'Loading the cast…'}</p>
      </div>
    );
  }

  const colours = castColours(view.characters);
  const ofCharacter = (slug: string) =>
    message && (message.where === slug || message.where.startsWith(`${slug}:`)) ? message : null;
  const addShown = addForm(view).shown;
  const owner = message?.where.split(':')[0];
  const placed = Boolean(message) && (
    view.characters.some((c) => c.slug === owner)
    || (message?.where === 'add' && addShown) || message?.where === 'shots' || message?.where === 'series');
  return (
    <div className={universe ? 'min-w-0' : card} data-testid="cast-panel">
      <h2 className="mb-1 text-[24px]">{universe ? 'Characters' : 'Cast'}</h2>
      <p className="mb-4 text-[13.5px] text-ink-soft">
        {universe
          ? 'The people (and creatures) of this Universe. Describe each one and add a picture; every episode in the Universe uses them, with the look locked once from an episode.'
          : 'The people (and creatures) in this video. Describe each one and add a picture, then lock their look with a character sheet so they look the same in every shot.'}
      </p>
      <p role="status" aria-live="polite" className="sr-only">{message?.text ?? ''}</p>
      {!placed && <CastMessage note={message} />}
      {!view.can_edit && (
        <p className="mb-4 text-[13.5px] text-ink-soft">
          {universe
            ? 'Only the owner of this Universe (or an admin) can change its characters.'
            : <>Only this video&apos;s owner (or an admin) can change its cast.</>}
        </p>
      )}
      {!universe && (
        <SeriesControl episodeId={episodeId} apiBase={base} view={view} busy={busy} act={act}
          note={placed && message?.where === 'series' ? message : null} />
      )}
      {!universe && view.sheet.dry_run && (
        <p className="mb-4 text-[12px] text-ink-soft">
          Preview mode: character sheets use free stand-in pictures, so you can try every step at no cost.
        </p>
      )}
      {view.characters.length === 0 ? (
        <p className="text-[13.5px] text-ink-soft">No one in the cast yet.</p>
      ) : (
        <div className={`grid items-start gap-5 sm:grid-cols-2 ${universe ? 'xl:grid-cols-3' : ''}`}>
          {view.characters.map((c, i) => (
            <CharacterCard key={c.character_id} apiBase={base} view={view} character={c}
              colour={colours[c.slug] ?? castColourAt(i)} index={i} busy={busy} act={act}
              note={ofCharacter(c.slug)} universe={universe} badge={badge} extra={extra} />
          ))}
        </div>
      )}
      {addShown && (
        <AddCharacter apiBase={base} view={view} busy={busy} act={act}
          note={placed && message?.where === 'add' ? message : null} />
      )}
      {!universe && (
        <ShotCast apiBase={base} view={view} busy={busy} act={act}
          note={placed && message?.where === 'shots' ? message : null} />
      )}
    </div>
  );
}
