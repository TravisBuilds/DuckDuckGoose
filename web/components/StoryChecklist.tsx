'use client';

import { useEffect, useState, type ReactNode } from 'react';
import { API_URL, apiFetch, postJson } from '@/lib/studio-api';
import { PICTURE_ACCEPT, emptyRights, pictureHint, pictureProblem, rightsFields, rightsProblem } from '@/lib/cast.mjs';
import type { PictureSizeLimits, RightsAnswers } from '@/lib/cast.mjs';
import {
  ADD_KINDS, KIND_CHIPS, KIND_LABELS, STATUS_TONES, countsLine, itemPhotos, itemStatus, listMadeMessage, listReady,
  makeListBlocker, makeListLabel, progressLine, progressPercent,
} from '@/lib/checklist.mjs';
import type { ChecklistItem, ChecklistKind, WorldCreation, WorldPhoto } from '@/lib/world.mjs';
import { input as field, label as fieldLabel } from '@/lib/ui';
import { ConfirmButton } from './ConfirmButton';
import { SizeNote, usePictureCheck } from './PictureSize';
import { RightsQuestions } from './RightsQuestions';
import { RefTile } from './playful';

/**
 * Story first (docs/WORLDS.md): the owner writes the story in plain words, the studio reads it into a
 * list of the references it needs (each character, place and prop, why, how many photos) and photos
 * upload onto each item. POST /api/worlds/{id}/story, /checklist, /checklist/items and /photos with
 * an item; all free. The API stays the authority: a refused step shows its reason.
 */

type Act = <T>(where: string, fn: () => Promise<T>, done: string | ((result: T) => string)) => Promise<T | null>;
type MadeList = { added: string[]; kept: string[]; removed: string[]; dropped: string[] };

const hint = 'text-[12.5px] text-ink-soft';
const STORY_PLACEHOLDER = 'e.g. Mira mends nets in a harbour town. One stormy night she finds a brass lantern in the old '
  + 'lighthouse and carries it to the night market, where her friend Tobin sells kites…';

export function StoryChecklist({ base, creation, pictureLimits, maxUploadBytes, busy, act, message }: {
  base: string;
  creation: WorldCreation;
  pictureLimits: PictureSizeLimits;
  maxUploadBytes: number;
  busy: boolean;
  act: Act;
  message: (where: string) => ReactNode;
}) {
  const saved = creation.story?.body ?? '';
  const [story, setStory] = useState<string | null>(null);
  useEffect(() => {
    if (story === null) setStory(saved);
  }, [story, saved]);
  const typed = story ?? saved;
  const changed = typed.trim() !== saved;
  const limits = creation.limits;
  const blocker = makeListBlocker({ saved, typed, storyMin: limits.story_min });
  const items = creation.checklist?.items ?? [];
  const progress = creation.checklist?.progress ?? null;
  const [open, setOpen] = useState<string | null>(null);
  const [kind, setKind] = useState<ChecklistKind>('character');
  const [name, setName] = useState('');
  const [why, setWhy] = useState('');

  const saveStory = () => act('story', () => postJson(`${base}/story`, { body: typed.trim() }), 'Story saved.');
  const makeList = async () => {
    if (changed && (await saveStory()) === null) return;
    await act('checklist', () => postJson<MadeList>(`${base}/checklist`, {}), listMadeMessage);
  };
  const addItem = async () => {
    const made = await act('checklist', () => postJson(`${base}/checklist/items`, { kind, name: name.trim(), why: why.trim() }),
      `${name.trim()} is on the list.`);
    if (made) {
      setName('');
      setWhy('');
    }
  };
  const full = items.length >= (limits.max_items ?? 16);

  return (
    <>
      <section className="pf-card p-5 sm:p-6" data-testid="story-card" aria-labelledby="story-title">
        <p className="text-[12px] font-extrabold uppercase tracking-wide text-blue-deep">First</p>
        <h2 id="story-title" className="text-[22px]">Start with your story</h2>
        <p className={hint}>
          Plain words, rough is fine: who is in it, where it happens and what happens. The studio reads it and lists
          the photos it needs. Only you and admins see it.
        </p>
        <label htmlFor="story-text" className={`${fieldLabel} mt-4`}>Your story</label>
        <textarea id="story-text" value={typed} rows={7} maxLength={limits.story_max ?? 4000}
          onChange={(e) => setStory(e.target.value)} placeholder={STORY_PLACEHOLDER} className={field}
          data-testid="story-text" />
        <div className="mt-1 flex flex-wrap justify-between gap-2 text-[12px] font-bold text-ink-soft">
          <span data-testid="story-saved" role="status">
            {changed ? 'Not saved yet' : saved ? 'Saved' : 'Nothing written yet'}
          </span>
          <span>{typed.length} of {limits.story_max ?? 4000}</span>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <button type="button" className="pf-btn pf-btn-primary" disabled={busy || Boolean(blocker)} onClick={makeList}
            aria-describedby={blocker ? 'story-make-why' : undefined} data-testid="checklist-make">
            {makeListLabel(creation.story)}
          </button>
          <button type="button" className="pf-btn pf-btn-quiet pf-btn-sm" disabled={busy || !changed} onClick={saveStory}
            title={changed ? undefined : 'Turns on once you change the story.'} data-testid="story-save">
            Save the story
          </button>
        </div>
        {blocker && <p id="story-make-why" className="pf-off-why" data-testid="story-make-why">{blocker}</p>}
        {!blocker && creation.story?.list_made && !creation.story.list_current && (
          <p className="mt-2 text-[12.5px] font-bold text-mustard-ink" data-testid="story-changed">
            Your story changed since the list was made. Update the list to match it; photos you added stay.
          </p>
        )}
        {message('story')}
      </section>

      {(items.length > 0 || creation.story?.list_made) && (
        <section className="pf-card mt-5 p-5 sm:p-6" data-testid="checklist" aria-labelledby="checklist-title">
          <div className="flex flex-wrap items-end justify-between gap-2">
            <div>
              <p className="text-[12px] font-extrabold uppercase tracking-wide text-blue-deep">Then</p>
              <h2 id="checklist-title" className="text-[22px]">Your reference list</h2>
            </div>
            <p className="text-[17px] font-extrabold" data-testid="checklist-progress" aria-live="polite">
              {progressLine(progress)}
            </p>
          </div>
          <div className="mt-2 h-3 overflow-hidden rounded-full bg-blue-soft" role="progressbar" aria-label="Required references ready"
            aria-valuemin={0} aria-valuemax={100} aria-valuenow={progressPercent(progress)}>
            <div className="h-full rounded-full bg-green transition-[width]" style={{ width: `${progressPercent(progress)}%` }} />
          </div>
          <p className={`${hint} mt-2`}>
            {listReady(progress)
              ? 'Every reference the story needs has its photos. Add more any time, or go on to the mood board.'
              : 'Add photos to each item. Small pictures from the web are fine: the studio sharpens them up.'}
          </p>
          {message('checklist')}
          <ul className="mt-4 space-y-3" data-testid="checklist-items">
            {items.map((item) => (
              <ChecklistRow key={item.item_id} item={item} photos={itemPhotos(item, creation.photos)} base={base}
                open={open === item.item_id} onOpen={() => setOpen(item.item_id)} onClose={() => setOpen(null)}
                busy={busy} act={act} pictureLimits={pictureLimits} maxUploadBytes={maxUploadBytes}
                canAdd={item.photo_count < (limits.max_photos_per_item ?? 6) && creation.photos.length < limits.max_photos} />
            ))}
          </ul>
          {items.length === 0 && (
            <p className="mt-3 text-[13.5px] font-bold text-ink-soft" data-testid="checklist-empty">
              Nothing on the list. Add what the story needs below, or make the list again.
            </p>
          )}
          {!full && (
            <div className="mt-5 rounded-inner bg-blue-soft p-4" data-testid="checklist-add">
              <h3 className="text-[16px]">Something missing?</h3>
              <div className="mt-2 grid gap-2 sm:grid-cols-[auto_1fr]">
                <select value={kind} onChange={(e) => setKind(e.target.value as ChecklistKind)} aria-label="What it is"
                  className={`${field} sm:w-auto`} data-testid="checklist-add-kind">
                  {ADD_KINDS.map((k) => <option key={k.kind} value={k.kind}>{k.label}</option>)}
                </select>
                <input value={name} onChange={(e) => setName(e.target.value)} maxLength={limits.item_name_max ?? 60}
                  aria-label="Name" placeholder="Name, e.g. Rain cloak" className={field} data-testid="checklist-add-name" />
              </div>
              <input value={why} onChange={(e) => setWhy(e.target.value)} maxLength={limits.why_max ?? 300}
                aria-label="Why the story needs it (optional)" placeholder="Why the story needs it (optional)"
                className={`${field} mt-2`} data-testid="checklist-add-why" />
              <button type="button" className="pf-btn pf-btn-soft pf-btn-sm mt-2" disabled={busy || !name.trim()} onClick={addItem}
                aria-describedby={name.trim() ? undefined : 'checklist-add-why-off'} data-testid="checklist-add-send">
                Add to the list
              </button>
              {!name.trim() && <p id="checklist-add-why-off" className="pf-off-why">Turns on once you name it.</p>}
            </div>
          )}
        </section>
      )}
    </>
  );
}

function ChecklistRow({ item, photos, base, open, onOpen, onClose, busy, act, pictureLimits, maxUploadBytes, canAdd }: {
  item: ChecklistItem;
  photos: WorldPhoto[];
  base: string;
  open: boolean;
  onOpen: () => void;
  onClose: () => void;
  busy: boolean;
  act: Act;
  pictureLimits: PictureSizeLimits;
  maxUploadBytes: number;
  canAdd: boolean;
}) {
  const status = itemStatus(item);
  const tone = STATUS_TONES[status.tone];
  return (
    <li data-testid={`checklist-item-${item.item_id}`} data-kind={item.kind} data-ready={item.ready ? 'true' : 'false'}
      data-optional={item.optional ? 'true' : 'false'}
      className={`rounded-inner border-l-[6px] bg-white p-4 shadow-card ${tone.edge}`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <span className={`pf-chip ${KIND_CHIPS[item.kind]} !px-2 !py-0.5 !text-[11px]`}>
            {KIND_LABELS[item.kind]}{item.optional ? ' · optional' : ''}
          </span>
          <h3 className="mt-1 break-words text-[18px]" data-testid={`checklist-name-${item.item_id}`}>{item.name}</h3>
        </div>
        <p className={`text-[13.5px] font-extrabold ${tone.text}`} data-testid={`checklist-status-${item.item_id}`}
          data-tone={status.tone}>
          {status.text}
        </p>
      </div>
      {item.why && <p className="mt-1 break-words text-[13.5px]" data-testid={`checklist-why-${item.item_id}`}>{item.why}</p>}
      <p className="mt-1 text-[12.5px] text-ink-soft" data-testid={`checklist-counts-${item.item_id}`}>
        {item.show ? `${item.show}. ` : ''}{countsLine(item)}.
      </p>
      {photos.length > 0 && (
        <ul className="mt-3 grid grid-cols-3 gap-2 sm:grid-cols-6" data-testid={`checklist-photos-${item.item_id}`}>
          {photos.map((p) => (
            <RefTile key={p.image_id} testId={`checklist-photo-${p.image_id}`} src={`${API_URL}${p.thumb_url}`}
              href={`${API_URL}${p.url}`} alt={`${item.name}: reference photo`}>
              <SizeNote text={p.size_note} testId={`checklist-photo-size-${p.image_id}`} />
              <ConfirmButton question="Remove this photo?" confirmLabel="Remove" disabled={busy}
                className="pf-btn pf-btn-quiet pf-btn-sm w-full !min-h-9 !px-2"
                onConfirm={() => act('checklist', () => postJson(`${base}/photos/${p.image_id}/delete`, {}), 'Photo removed.')}>
                Remove
              </ConfirmButton>
            </RefTile>
          ))}
        </ul>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {canAdd && !open && (
          <button type="button" className="pf-btn pf-btn-soft pf-btn-sm" disabled={busy} onClick={onOpen}
            data-testid={`checklist-add-photo-${item.item_id}`}>
            {photos.length > 0 ? 'Add another photo' : 'Add a photo'}
          </button>
        )}
        <ConfirmButton question={`Take ${item.name} off the list? Its photos stay with your references.`}
          confirmLabel="Take it off" disabled={busy} className="pf-btn pf-btn-quiet pf-btn-sm"
          testId={`checklist-remove-${item.item_id}`}
          onConfirm={() => act('checklist', () => postJson(`${base}/checklist/items/${item.item_id}/delete`, {}),
            `${item.name} is off the list.`)}>
          Take off the list
        </ConfirmButton>
      </div>
      {open && (
        <ItemUpload item={item} base={base} busy={busy} act={act} pictureLimits={pictureLimits}
          maxUploadBytes={maxUploadBytes} onDone={onClose} />
      )}
    </li>
  );
}

function ItemUpload({ item, base, busy, act, pictureLimits, maxUploadBytes, onDone }: {
  item: ChecklistItem;
  base: string;
  busy: boolean;
  act: Act;
  pictureLimits: PictureSizeLimits;
  maxUploadBytes: number;
  onDone: () => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [rights, setRights] = useState<RightsAnswers>(emptyRights());
  const size = usePictureCheck(file, pictureLimits);
  const issue = file ? (pictureProblem(file, maxUploadBytes) ?? size.problem) : null;
  const rightsIssue = rightsProblem(rights);
  const id = `checklist-file-${item.item_id}`;
  const send = async () => {
    if (!file) return;
    const form = new FormData();
    form.append('image_file', file);
    form.append('item_id', item.item_id);
    for (const [key, value] of rightsFields(rights)) form.append(key, value);
    const made = await act('checklist', () => apiFetch<{ duplicate: boolean }>(`${base}/photos`, { method: 'POST', body: form }),
      (r) => (r.duplicate ? `That photo was here already; it is on ${item.name} now.` : `Photo added to ${item.name}.`));
    if (made) onDone();
  };
  return (
    <div className="mt-3 rounded-inner border-[3px] border-dashed border-blue/40 bg-blue-soft p-4" data-testid={`checklist-upload-${item.item_id}`}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <label htmlFor={id} className="pf-btn pf-btn-primary pf-btn-sm cursor-pointer self-start focus-within:outline-3 focus-within:outline-blue-deep">
          Choose a photo
          <input id={id} type="file" accept={PICTURE_ACCEPT} className="sr-only" data-testid={id}
            aria-describedby={`${id}-hint`} onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </label>
        <p id={`${id}-hint`} className={hint}>{pictureHint(pictureLimits)}</p>
      </div>
      {file && <p className="mt-2 break-words text-[13px] font-bold">Chosen: {file.name}</p>}
      {issue && <p className="mt-1 text-[12.5px] font-bold text-coral-ink" role="alert">{issue}</p>}
      {!issue && <SizeNote text={size.note} testId={`checklist-size-${item.item_id}`} className="mt-1" />}
      {file && (
        <RightsQuestions rights={rights} onChange={setRights} group={`checklist-rights-${item.item_id}`}
          testId={(what) => `checklist-${what}-${item.item_id}`} />
      )}
      {file && !issue && rightsIssue && <p className="mt-2 text-[12.5px] font-bold text-mustard-ink">{rightsIssue}</p>}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button type="button" className="pf-btn pf-btn-primary pf-btn-sm" disabled={busy || !file || Boolean(issue) || Boolean(rightsIssue)}
          aria-describedby={file ? undefined : `${id}-off`} onClick={send} data-testid={`checklist-send-${item.item_id}`}>
          Add to {item.name}
        </button>
        <button type="button" className="pf-btn pf-btn-quiet pf-btn-sm" onClick={onDone}>Cancel</button>
      </div>
      {!file && <p id={`${id}-off`} className="pf-off-why">Turns on once you choose a photo.</p>}
    </div>
  );
}
