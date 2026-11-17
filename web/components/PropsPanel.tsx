'use client';

import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { API_URL, ApiError, apiFetch, postJson } from '@/lib/studio-api';
import { PICTURE_ACCEPT, emptyRights, pictureProblem, rightsFields, rightsProblem } from '@/lib/cast.mjs';
import type { RightsAnswers } from '@/lib/cast.mjs';
import { KINDS, KIND_TEXT, addPropProblem, itemPhotos, propAddForm, withPhotoFields } from '@/lib/props.mjs';
import type { PropItem, PropKind, PropPhoto, PropsView } from '@/lib/props.mjs';
import { btnPrimary, btnSecondary, card, chip, input as field, tones } from '@/lib/ui';
import { ConfirmButton } from './ConfirmButton';
import { RightsQuestions } from './RightsQuestions';
import { ThingCard } from './playful';

/**
 * The video's props and places (its series' when it is in one), or with `universe` those of a
 * Universe: a name, a short description and reference photos for each, so a prop or place looks the
 * same in every shot it is picked for. The picks are made per shot in the shot plan (ShotPlanEditor).
 * Every action is an API route under `apiBase` (/api/episodes/{id} or /api/worlds/{id};
 * api/prop_routes.py), which stays the authority: a refused action shows the API's reason next to it.
 */

type Note = { tone: 'ok' | 'problem'; text: string; where: string };
/** Runs a change, then shows `done` and reloads the panel. `onDone` runs with the result as soon as the change
 * succeeds, in the same render as the message, so a form reset never lands on what was typed during the reload. */
type Act = <T>(fn: () => Promise<T>, done: string, where: string, onDone?: (result: T) => void) => Promise<T | null>;

const UNREACHABLE = 'The studio could not be reached. Check your connection and try again.';
const hint = 'text-[12px] text-ink-soft';
const smallBtn = 'pf-btn pf-btn-quiet pf-btn-sm !min-h-9 !px-3 !text-[12.5px]';

function problemText(e: unknown): string {
  return e instanceof ApiError ? e.message : UNREACHABLE;
}

function megabytes(bytes: number): number {
  return Math.round(bytes / 1_048_576);
}

function Message({ note }: { note: Note | null }) {
  if (!note) return null;
  return (
    <p data-testid="props-message" data-tone={note.tone}
      className={`mt-3 rounded-inner p-3 text-[13px] font-bold ${note.tone === 'ok' ? 'bg-green-soft text-green-ink' : 'bg-coral-soft text-coral-ink'}`}>
      {note.text}
    </p>
  );
}

function Photo({ photo, alt, size = 'h-24 w-24 sm:h-28 sm:w-28' }: { photo: PropPhoto; alt: string; size?: string }) {
  return (
    <a href={`${API_URL}${photo.url}`} target="_blank" rel="noreferrer" title="Open the full photo"
      className="block shrink-0 rounded-[12px]">
      <img src={`${API_URL}${photo.thumb_url}`} alt={alt} loading="lazy"
        className={`${size} rounded-[12px] bg-mist object-cover shadow-card`} />
    </a>
  );
}

function photoHint(view: PropsView) {
  return `PNG, JPG or WebP, at least ${view.limits.min_image_px} px on each side, up to ${megabytes(view.limits.max_upload_bytes)} MB.`;
}

/** Another photo of the same prop or place (another angle, a close-up), with the rights questions. */
function AddPhoto({ apiBase, view, item, busy, act }: {
  apiBase: string; view: PropsView; item: PropItem; busy: boolean; act: Act;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [rights, setRights] = useState<RightsAnswers>(emptyRights());
  const [inputKey, setInputKey] = useState(0);
  const fileIssue = file ? pictureProblem(file, view.limits.max_upload_bytes) : null;
  const rightsIssue = rightsProblem(rights);
  const send = async () => {
    if (!file) return;
    const form = new FormData();
    form.append('image_file', file);
    for (const [key, value] of rightsFields(rights)) form.append(key, value);
    await act(() => apiFetch(`${apiBase}/props/${item.prop_id}/photos`, { method: 'POST', body: form }),
      `Photo of ${item.name} added.`, item.slug, () => {
        setFile(null);
        setRights(emptyRights());
        setInputKey((k) => k + 1);
      });
  };
  const first = itemPhotos(item).length === 0;
  return (
    <details className="mt-3 rounded-inner bg-page p-3 text-left" data-testid={`prop-add-photo-${item.slug}`}
      open={first || undefined}>
      <summary className="cursor-pointer rounded text-[13px] font-extrabold">
        {first ? 'Add a photo' : 'Add another photo'}
      </summary>
      <p className={`${hint} mt-2`} id={`prop-photo-hint-${item.slug}`}>
        {photoHint(view)} {first ? 'A clear photo of just this one works best.' : 'Another angle or a close-up helps the studio get it right.'}
      </p>
      <input key={inputKey} type="file" accept={PICTURE_ACCEPT} aria-label={`Photo of ${item.name}`}
        aria-describedby={`prop-photo-hint-${item.slug}`} onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        className="mt-2 block w-full text-[13px]" data-testid={`prop-photo-file-${item.slug}`} />
      {fileIssue && <p className="mt-1 text-[12px] font-bold text-coral-ink">{fileIssue}</p>}
      <RightsQuestions rights={rights} onChange={setRights} group={`prop-rights-${item.slug}`}
        testId={(what) => `prop-${what}-${item.slug}`} />
      {file && !fileIssue && rightsIssue && <p className="mt-2 text-[12px] font-bold text-mustard-ink">{rightsIssue}</p>}
      <button type="button" onClick={send} disabled={busy || !file || !!fileIssue || !!rightsIssue}
        className={`${btnSecondary} pf-btn-sm mt-3`} data-testid={`prop-photo-send-${item.slug}`}>
        Upload photo
      </button>
    </details>
  );
}

function ItemCard({ apiBase, view, item, index, busy, act, note, extra }: {
  apiBase: string; view: PropsView; item: PropItem; index: number; busy: boolean; act: Act; note: Note | null;
  extra?: (item: PropItem) => ReactNode;
}) {
  const photos = itemPhotos(item);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState({ name: item.name, description: item.description });
  const kind = KIND_TEXT[item.kind] ?? KIND_TEXT.prop;
  const save = async () => {
    const name = form.name.trim();
    await act(() => postJson(`${apiBase}/props/${item.prop_id}`,
      { name, description: form.description.trim() }), `${name} saved.`, item.slug, () => setEditing(false));
  };
  const edit = () => {
    setForm({ name: item.name, description: item.description });
    setEditing(true);
  };
  return (
    <li className="min-w-0" data-testid={`prop-${item.slug}`} data-kind={item.kind}>
      <ThingCard kind={item.kind === 'place' ? 'place' : 'prop'} index={index} photos={photos.length}
        picture={photos[0] ? { src: `${API_URL}${photos[0].thumb_url}`, href: `${API_URL}${photos[0].url}`, alt: `${item.name}: main photo` } : null}
        title={<h3 className="break-words text-[16px]">{item.name}</h3>}>
      <div className="mt-2 text-left">
        <p className="break-words text-[13px]" data-testid={`prop-description-${item.slug}`}>
          {item.description || 'No description yet: add what it looks like, so every shot gets it right.'}
        </p>
        {extra?.(item)}
      </div>

      {photos.length > 1 && (
        <ul className="mt-3 flex flex-wrap gap-3 text-left" aria-label={`Photos of ${item.name}`}>
          {photos.map((photo, i) => (
            <li key={photo.image_id} className="w-24 sm:w-28" data-testid={`prop-photo-${photo.image_id}`}>
              <Photo photo={photo} alt={i === 0 ? `${item.name}: main photo` : `${item.name}: photo ${i + 1}`} />
              {view.can_edit && (
                <div className="mt-1 flex flex-col gap-1">
                  {photo.is_primary ? (
                    <span className="pf-chip pf-chip-lock justify-center">Main photo</span>
                  ) : (
                    <button type="button" className={smallBtn} disabled={busy}
                      onClick={() => act(() => postJson(`${apiBase}/props/${item.prop_id}/photos/${photo.image_id}/primary`, {}),
                        'Main photo changed.', item.slug)}>
                      Use as main
                    </button>
                  )}
                  <ConfirmButton question="Remove this photo?" confirmLabel="Remove" disabled={busy} className={smallBtn}
                    testId={`prop-photo-remove-${photo.image_id}`}
                    onConfirm={() => act(() => postJson(`${apiBase}/props/${item.prop_id}/photos/${photo.image_id}/delete`, {}),
                      'Photo removed.', item.slug)}>
                    Remove
                  </ConfirmButton>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {view.can_edit && editing && (
        <div className="mt-3 space-y-2 text-left">
          <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} maxLength={view.limits.name_max}
            aria-label="Name" className={field} data-testid={`prop-edit-name-${item.slug}`} />
          <textarea value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })}
            maxLength={view.limits.description_max} rows={3} aria-label="What it looks like" className={field}
            data-testid={`prop-edit-description-${item.slug}`} />
          <div className="flex flex-wrap gap-2">
            <button type="button" className={btnPrimary} disabled={busy || !form.name.trim()} onClick={save}
              data-testid={`prop-edit-save-${item.slug}`}>
              Save
            </button>
            <button type="button" className={btnSecondary} onClick={() => setEditing(false)}>Cancel</button>
          </div>
        </div>
      )}
      {view.can_edit && !editing && (
        <div className="mt-3 flex flex-wrap gap-2">
          <button type="button" className={smallBtn} disabled={busy} onClick={edit} data-testid={`prop-edit-${item.slug}`}>
            Edit name or description
          </button>
          {photos.length === 1 && (
            <ConfirmButton question="Remove this photo?" confirmLabel="Remove" disabled={busy} className={smallBtn}
              testId={`prop-photo-remove-${photos[0].image_id}`}
              onConfirm={() => act(() => postJson(`${apiBase}/props/${item.prop_id}/photos/${photos[0].image_id}/delete`, {}),
                'Photo removed.', item.slug)}>
              Remove the photo
            </ConfirmButton>
          )}
          <ConfirmButton question={`Remove ${item.name} and its photos?`} confirmLabel="Remove" disabled={busy}
            className={smallBtn} testId={`prop-remove-${item.slug}`}
            onConfirm={() => act(() => postJson(`${apiBase}/props/${item.prop_id}/delete`, {}),
              `${item.name} removed.`, item.slug)}>
            Remove {kind.label.toLowerCase()}
          </ConfirmButton>
        </div>
      )}
      {view.can_edit && photos.length < view.limits.max_photos_per_item && (
        <AddPhoto apiBase={apiBase} view={view} item={item} busy={busy} act={act} />
      )}
      <Message note={note} />
      </ThingCard>
    </li>
  );
}

/**
 * Add a prop or place: what it is, a photo (with the same checks and rights questions as a cast
 * picture), a name and what it looks like; or without a photo. The photo comes first.
 */
function AddItem({ apiBase, view, busy, act, note }: {
  apiBase: string; view: PropsView; busy: boolean; act: Act; note: Note | null;
}) {
  const empty = { name: '', description: '' };
  const [kind, setKind] = useState<PropKind | ''>('');
  const [form, setForm] = useState(empty);
  const [file, setFile] = useState<File | null>(null);
  const [rights, setRights] = useState<RightsAnswers>(emptyRights());
  const [inputKey, setInputKey] = useState(0);
  const { first, title } = propAddForm(view);
  const name = form.name.trim();
  const fileIssue = file ? pictureProblem(file, view.limits.max_upload_bytes) : null;
  const photoIssue = addPropProblem(kind, name, file, rights, view.limits.max_upload_bytes);
  const what = kind || 'prop or place';

  const added = () => {
    setKind('');
    setForm(empty);
    setFile(null);
    setRights(emptyRights());
    setInputKey((k) => k + 1);
  };
  const addWithPhoto = async () => {
    if (!file || photoIssue || !kind) return;
    const body = new FormData();
    body.append('image_file', file);
    for (const [key, value] of withPhotoFields({ kind, ...form }, rights)) body.append(key, value);
    await act(() => apiFetch(`${apiBase}/props/with-photo`, { method: 'POST', body }),
      `${name} added with its photo. Pick it on a shot in the shot plan.`, 'add', added);
  };
  const addWithoutPhoto = async () => {
    if (!kind) return;
    await act(() => postJson(`${apiBase}/props`, { kind, name, description: form.description.trim() }),
      `${name} added. Add a photo so the studio knows what it looks like.`, 'add', added);
  };
  return (
    <div data-testid="props-add" data-first={first ? 'true' : 'false'}
      className={`mt-5 rounded-card border-[3px] border-dashed p-5 ${first ? 'border-blue/40 bg-blue-soft' : 'border-line-strong'}`}>
      <h3 className="mb-1 text-[17px]" data-testid="props-add-title">{title}</h3>
      <fieldset className="mt-2">
        <legend className="mb-2 text-[13px] font-extrabold">What is it?</legend>
        <div className="grid gap-2 sm:grid-cols-2">
          {KINDS.map((k) => (
            <label key={k} className={`flex min-h-11 cursor-pointer items-start gap-2 rounded-inner border-2 bg-white p-3 text-[13.5px] ${kind === k ? 'border-blue-deep' : 'border-transparent shadow-card'}`}>
              <input type="radio" name="props-add-kind" value={k} checked={kind === k} onChange={() => setKind(k)}
                className="mt-0.5" data-testid={`props-add-kind-${k}`} />
              <span>
                <span className="block font-semibold">{KIND_TEXT[k].choice}</span>
                <span className={hint}>{KIND_TEXT[k].hint}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>
      <label htmlFor="props-add-file" className="mt-3 block text-[13px] font-extrabold">{first ? 'Photo' : 'Photo (recommended)'}</label>
      <p id="props-add-file-hint" className={`${hint} mb-1`}>
        {photoHint(view)} A clear photo of just this {what} works best.
      </p>
      <input id="props-add-file" key={inputKey} type="file" accept={PICTURE_ACCEPT}
        onChange={(e) => setFile(e.target.files?.[0] ?? null)} aria-describedby="props-add-file-hint"
        className="block w-full text-[13px]" data-testid="props-add-file" />
      {fileIssue && <p className="mt-1 text-[12px] font-bold text-coral-ink">{fileIssue}</p>}
      <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} maxLength={view.limits.name_max}
        placeholder={kind === 'place' ? 'Name, e.g. Harbour cabin' : 'Name, e.g. Red umbrella'} aria-label="Name"
        className={`${field} mt-3`} data-testid="props-add-name" />
      <textarea value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })}
        maxLength={view.limits.description_max} rows={3} aria-label="What it looks like" data-testid="props-add-description"
        placeholder={kind === 'place'
          ? 'What the place looks like, the same in every shot: the room or landscape, light, colours, what is always there'
          : 'What it looks like, the same in every shot: shape, size, colours, materials'}
        className={`${field} mt-2`} />
      {file && (
        <RightsQuestions rights={rights} onChange={setRights} group="props-rights-add" testId={(w) => `props-add-${w}`} />
      )}
      {file && !fileIssue && photoIssue && (
        <p className="mt-2 text-[12px] font-bold text-mustard-ink" data-testid="props-add-problem">{photoIssue}</p>
      )}
      <div className="mt-3 flex flex-wrap gap-2">
        <button type="button" onClick={addWithPhoto} disabled={busy || !!photoIssue} className={btnPrimary}
          data-testid="props-add-with-photo">
          Add with this photo
        </button>
        <button type="button" onClick={addWithoutPhoto} disabled={busy || !kind || !name} className={btnSecondary}
          data-testid="props-add-button">
          Add without a photo
        </button>
      </div>
      <Message note={note} />
    </div>
  );
}

/** The Universe page's props and places (its view of GET /api/worlds/{id}) and how to reload them after a change. */
export interface UniverseProps {
  view: PropsView;
  reload: () => Promise<void>;
}

/** onChange: after every change, so the shot plan picker shows the new list. extra: more about an item, under its photo count. */
export default function PropsPanel({ episodeId = '', apiBase, universe = null, extra, onChange }: {
  episodeId?: string; apiBase?: string; universe?: UniverseProps | null; extra?: (item: PropItem) => ReactNode;
  onChange?: () => void;
}) {
  const base = apiBase ?? `/api/episodes/${episodeId}`;
  const [loaded, setView] = useState<PropsView | null>(null);
  const view = universe ? universe.view : loaded;
  const [loadIssue, setLoadIssue] = useState<string | null>(null);
  const [message, setMessage] = useState<Note | null>(null);
  const [busy, setBusy] = useState(false);

  const reloadUniverse = universe?.reload;
  const load = useCallback(async () => {
    if (reloadUniverse) {
      await reloadUniverse();
      return;
    }
    try {
      setView(await apiFetch<PropsView>(`${base}/props`));
      setLoadIssue(null);
    } catch (e) {
      setLoadIssue(e instanceof ApiError ? e.message : 'The props and places could not be loaded right now.');
    }
  }, [base, reloadUniverse]);

  const inUniverse = Boolean(universe);
  useEffect(() => {
    if (!inUniverse) load();
  }, [load, inUniverse]);

  const act: Act = async (fn, done, where, onDone) => {
    setBusy(true);
    setMessage(null);
    try {
      const result = await fn();
      onDone?.(result);
      setMessage({ tone: 'ok', text: done, where });
      onChange?.();
      return result;
    } catch (e) {
      setMessage({ tone: 'problem', text: problemText(e), where });
      return null;
    } finally {
      setBusy(false);
      await load();
    }
  };

  if (!view) {
    return (
      <div className={universe ? 'min-w-0' : card} data-testid="props-panel">
        <h2 className="mb-1 text-[24px]">{universe ? 'Places & props' : <>Props &amp; places</>}</h2>
        <p className="text-[13.5px] text-ink-soft">{loadIssue ?? 'Loading the props and places…'}</p>
      </div>
    );
  }

  const add = propAddForm(view);
  const shownIn = (where: string) => (message?.where === where ? message : null);
  const placed = Boolean(message) && ((message?.where === 'add' && add.shown) || view.items.some((i) => i.slug === message?.where));
  return (
    <div className={universe ? 'min-w-0' : card} data-testid="props-panel">
      <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-[24px]">{universe ? 'Places & props' : <>Props &amp; places</>}</h2>
        {view.items.length > 0 && (
          <span className={`${chip} ${tones.wait}`} data-testid="props-count">
            {view.items.length} of {view.limits.max_items}
          </span>
        )}
      </div>
      <p className="mb-4 text-[13.5px] text-ink-soft">
        {universe
          ? 'The places and things of this Universe, with a photo of each. Every episode in it can pick them on a shot in its shot plan: the studio sends their photos with that shot and adds your description to its prompt.'
          : 'The things and settings that have to look right, with a photo of each. Pick them on each shot in the shot plan: the studio sends their photos with that shot and adds your description to its prompt.'}
      </p>
      {!universe && view.series && (
        <p className={`${hint} mb-3`} data-testid="props-series">
          Shared by every video in the Universe “{view.series.name}”.
        </p>
      )}
      <p role="status" aria-live="polite" className="sr-only">{message?.text ?? ''}</p>
      {!placed && <Message note={message} />}
      {!view.can_edit && (
        <p className="mb-3 text-[13.5px] text-ink-soft" data-testid="props-read-only">
          {universe
            ? 'Only the owner of this Universe (or an admin) can change its places and props.'
            : view.series
            ? 'Only the owner of this Universe (or an admin) can change its props and places.'
            : 'Only this video’s owner (or an admin) can change its props and places.'}
        </p>
      )}
      {view.items.length === 0 ? (
        <div className="rounded-card border-2 border-dashed border-line-strong p-5 text-[13.5px]"
          data-testid="props-empty">
          <p className="font-semibold">No props or places yet.</p>
          <p className="mt-1">
            Does a shot need a thing to look a certain way, or happen in the same place every time? Add it here with a
            photo, for example:
          </p>
          <ul className="mt-2 list-disc space-y-1 pl-5">
            <li><span className="font-medium">A prop</span>: {KIND_TEXT.prop.hint}.</li>
            <li><span className="font-medium">A place</span>: {KIND_TEXT.place.hint}.</li>
          </ul>
          <p className="mt-2">Shots you pick nothing for are made just as before.</p>
        </div>
      ) : (
        <ul className={`grid gap-4 sm:grid-cols-2 ${universe ? 'xl:grid-cols-3' : ''}`} data-testid="props-list">
          {view.items.map((item, i) => (
            <ItemCard key={item.prop_id} apiBase={base} view={view} item={item} index={i} busy={busy} act={act}
              note={placed ? shownIn(item.slug) : null} extra={extra} />
          ))}
        </ul>
      )}
      {add.shown && <AddItem apiBase={base} view={view} busy={busy} act={act} note={placed ? shownIn('add') : null} />}
    </div>
  );
}
