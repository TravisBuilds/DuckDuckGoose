'use client';

import { useState } from 'react';
import { API_URL, ApiError, apiFetch, postJson } from '@/lib/studio-api';
import { formatUsd } from '@/lib/money';
import { PICTURE_ACCEPT, emptyRights, pictureProblem, rightsFields, rightsProblem } from '@/lib/cast.mjs';
import type { RightsAnswers } from '@/lib/cast.mjs';
import type { BoardEstimate, WorldBoard, WorldView } from '@/lib/world.mjs';
import { btnPrimary, btnSecondary, card, chip, focusRing, input as field, tones } from '@/lib/ui';
import { ConfirmButton } from './ConfirmButton';
import { RightsQuestions } from './RightsQuestions';

/**
 * How a Universe is made (its owner and admins): references (notes on the premise, characters,
 * places and tone, and photos), a mood board made from them for the price shown first, approved or
 * redone with feedback, then the bible: characters, places and props written from the approved board.
 * Every step is an API route (api/world_routes.py), which stays the authority: a refused step shows
 * the API's reason.
 */

const UNREACHABLE = 'The studio could not be reached. Check your connection and try again.';
const TOPIC_TEXT: Record<string, { label: string; placeholder: string }> = {
  premise: { label: 'Premise', placeholder: 'What the Universe is about, who wants what, what keeps happening' },
  characters: { label: 'Characters', placeholder: 'Who lives here: names, ages, how they look and act' },
  places: { label: 'Places', placeholder: 'Where it happens: rooms, streets, landscapes, the weather' },
  tone: { label: 'Tone', placeholder: 'How it feels: funny, cosy, eerie; the colours and light' },
};
const hint = 'text-xs text-gray-600 dark:text-gray-300';

type Note = { tone: 'ok' | 'problem'; text: string; where: string };

function problemText(e: unknown): string {
  return e instanceof ApiError ? e.message : UNREACHABLE;
}

function megabytes(bytes: number): number {
  return Math.round(bytes / 1_048_576);
}

function Message({ note, where }: { note: Note | null; where: string }) {
  if (!note || note.where !== where) return null;
  return (
    <p data-testid={`creation-message-${where}`} data-tone={note.tone} role={note.tone === 'problem' ? 'alert' : 'status'}
      className={`mt-3 rounded-lg p-3 text-sm ${note.tone === 'ok' ? 'bg-green-50 text-green-800 dark:bg-green-900/20 dark:text-green-200' : 'bg-red-50 text-red-800 dark:bg-red-900/20 dark:text-red-200'}`}>
      {note.text}
    </p>
  );
}

function StepTitle({ n, title, status }: { n: number; title: string; status: 'done' | 'current' | 'todo' }) {
  const tone = status === 'done' ? tones.ok : status === 'current' ? tones.todo : tones.wait;
  return (
    <div className="flex flex-wrap items-center gap-2" data-testid={`creation-step-${n}`} data-status={status}>
      <h3 className="text-base font-semibold text-gray-900 dark:text-white">{n}. {title}</h3>
      <span className={`${chip} ${tone}`}>{status === 'done' ? 'Done' : status === 'current' ? 'Now' : 'Later'}</span>
    </div>
  );
}

function BoardImages({ board }: { board: WorldBoard }) {
  return (
    <ul className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4" data-testid={`board-images-${board.version}`}>
      {board.images.map((image) => (
        <li key={image.image_id}>
          <a href={`${API_URL}${image.url}`} target="_blank" rel="noreferrer" className={`block rounded-lg ${focusRing}`}
            title="Open the full picture">
            <img src={`${API_URL}${image.thumb_url}`} alt={`Mood board ${board.version}, picture ${image.position + 1}`}
              loading="lazy" className="aspect-square w-full rounded-lg border border-gray-300 bg-gray-100 object-cover dark:border-gray-600 dark:bg-gray-900" />
          </a>
          {image.placeholder && <p className={`${hint} mt-1`}>Stand-in (preview)</p>}
        </li>
      ))}
    </ul>
  );
}

export function UniverseCreation({ view, reload }: { view: WorldView; reload: () => Promise<void> }) {
  const creation = view.creation;
  const seriesId = view.world.series_id;
  const base = `/api/worlds/${encodeURIComponent(seriesId)}`;
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<Note | null>(null);
  const [topic, setTopic] = useState('premise');
  const [text, setText] = useState('');
  const [editing, setEditing] = useState<{ id: string; body: string } | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [caption, setCaption] = useState('');
  const [rights, setRights] = useState<RightsAnswers>(emptyRights());
  const [inputKey, setInputKey] = useState(0);
  const [feedback, setFeedback] = useState('');
  const [estimate, setEstimate] = useState<{ feedback: string; result: BoardEstimate } | null>(null);
  const [bible, setBible] = useState<{ created: Record<string, string[]>; skipped: string[]; dropped: string[] } | null>(null);
  if (!creation) return null;

  const act = async <T,>(where: string, fn: () => Promise<T>, done: string | ((result: T) => string)): Promise<T | null> => {
    setBusy(true);
    setNote(null);
    try {
      const result = await fn();
      setNote({ tone: 'ok', text: typeof done === 'string' ? done : done(result), where });
      await reload();
      return result;
    } catch (e) {
      setNote({ tone: 'problem', text: problemText(e), where });
      await reload().catch(() => undefined);
      return null;
    } finally {
      setBusy(false);
    }
  };

  const boards = creation.boards;
  const latest = boards.length > 0 ? boards[boards.length - 1] : null;
  const approved = boards.find((b) => b.status === 'approved') ?? null;
  const bibleDone = Boolean(approved && view.world.profile.bible_board_id === approved.board_id);
  const references = creation.notes.length + creation.photos.length;
  const status = (n: number): 'done' | 'current' | 'todo' => {
    const current = references === 0 ? 1 : !latest ? 2 : !approved ? 3 : !bibleDone ? 4 : 5;
    return n < current ? 'done' : n === current ? 'current' : 'todo';
  };
  const limits = creation.limits;
  const fileIssue = file ? pictureProblem(file, view.limits.max_upload_bytes) : null;
  const rightsIssue = rightsProblem(rights);
  const waiting = latest?.status === 'ready' ? latest : null;

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
    const made = await act('board', () => postJson(`${base}/board`, {
      feedback: estimate.feedback, approved_total_usd_micros: estimate.result.total_usd_micros,
    }), estimate.feedback ? 'The new mood board is ready below.' : 'Your mood board is ready below.');
    if (made) {
      setEstimate(null);
      setFeedback('');
    }
  };
  const approve = (board: WorldBoard) => act('board', () => postJson(`${base}/board/${board.board_id}/approve`, {}),
    'Mood board approved. Next: the bible.');
  const writeBible = async () => {
    const made = await act('bible', () => postJson<{ bible: { created: Record<string, string[]>; skipped: string[]; dropped: string[] } }>(
      `${base}/bible`, {}), (r) => {
      const c = r.bible.created;
      return `Added ${c.characters.length} character${c.characters.length === 1 ? '' : 's'}, ${c.places.length} place${c.places.length === 1 ? '' : 's'} and ${c.props.length} prop${c.props.length === 1 ? '' : 's'}.`;
    });
    if (made) setBible(made.bible);
  };

  const estimateBox = estimate && (
    <div className="mt-3 rounded-lg border border-yellow-200 bg-yellow-50 p-3 dark:border-yellow-800 dark:bg-yellow-900/20"
      data-testid="board-estimate">
      <p className="text-sm font-semibold text-yellow-900 dark:text-yellow-200">
        {estimate.result.dry_run
          ? `${estimate.result.summary} (a live board would cost ${formatUsd(estimate.result.total_usd_micros)})`
          : `${estimate.result.summary}: ${formatUsd(estimate.result.total_usd_micros)}`}
      </p>
      {!estimate.result.dry_run && (
        <p className="mt-1 text-xs text-yellow-800 dark:text-yellow-300">
          The studio checks the price and your balance again before each picture.
        </p>
      )}
      <div className="mt-2 flex flex-wrap gap-2">
        <button type="button" className={btnPrimary} disabled={busy} onClick={makeBoard} data-testid="board-make">
          {estimate.feedback ? 'Make the new mood board' : 'Make the mood board'}
        </button>
        <button type="button" className={btnSecondary} disabled={busy} onClick={() => setEstimate(null)}>Cancel</button>
      </div>
    </div>
  );

  return (
    <section className={card} data-testid="universe-creation" aria-labelledby="creation-title">
      <h2 id="creation-title" className="text-xl font-semibold">Build this Universe</h2>
      <p className="mt-1 text-sm text-gray-700 dark:text-gray-300">
        Only you (and admins) see this part. Add references, approve a mood board, and the studio writes the
        characters and places for you. You can also add them yourself below at any time.
      </p>

      <div className="mt-5 space-y-6">
        <div data-testid="creation-references">
          <StepTitle n={1} title="References" status={status(1)} />
          <p className={`${hint} mt-1`}>A few words about each, and photos of anything that sets the look (your own or ones you have the rights to).</p>
          {creation.notes.length > 0 && (
            <ul className="mt-3 space-y-2" data-testid="creation-notes">
              {creation.notes.map((n) => (
                <li key={n.note_id} className="rounded-lg border border-gray-200 p-3 text-sm dark:border-gray-700" data-testid={`creation-note-${n.note_id}`}>
                  <span className={`${chip} ${tones.wait} mr-2`}>{TOPIC_TEXT[n.topic]?.label ?? n.topic}</span>
                  {editing?.id === n.note_id ? (
                    <div className="mt-2 space-y-2">
                      <textarea value={editing.body} onChange={(e) => setEditing({ id: n.note_id, body: e.target.value })}
                        maxLength={limits.note_max} rows={3} aria-label="Note" className={field} />
                      <div className="flex gap-2">
                        <button type="button" className={btnPrimary} disabled={busy || !editing.body.trim()} onClick={saveNote}>Save</button>
                        <button type="button" className={btnSecondary} onClick={() => setEditing(null)}>Cancel</button>
                      </div>
                    </div>
                  ) : (
                    <>
                      <span className="whitespace-pre-line break-words">{n.body}</span>
                      <div className="mt-2 flex flex-wrap gap-2">
                        <button type="button" className={btnSecondary} disabled={busy}
                          onClick={() => setEditing({ id: n.note_id, body: n.body })}>
                          Edit
                        </button>
                        <ConfirmButton question="Remove this note?" confirmLabel="Remove" disabled={busy} className={btnSecondary}
                          onConfirm={() => removeNote(n.note_id)}>
                          Remove
                        </ConfirmButton>
                      </div>
                    </>
                  )}
                </li>
              ))}
            </ul>
          )}
          {creation.notes.length < limits.max_notes && (
            <div className="mt-3 rounded-lg bg-gray-50 p-3 dark:bg-gray-900/40">
              <label htmlFor="creation-topic" className="block text-sm font-medium">About</label>
              <select id="creation-topic" value={topic} onChange={(e) => setTopic(e.target.value)} className={`${field} mt-1 sm:w-auto`}
                data-testid="creation-topic">
                {creation.topics.map((t) => <option key={t} value={t}>{TOPIC_TEXT[t]?.label ?? t}</option>)}
              </select>
              <textarea value={text} onChange={(e) => setText(e.target.value)} maxLength={limits.note_max} rows={3}
                aria-label="Note" placeholder={TOPIC_TEXT[topic]?.placeholder} className={`${field} mt-2`}
                data-testid="creation-note-text" />
              <button type="button" className={`${btnSecondary} mt-2`} disabled={busy || !text.trim()} onClick={addNote}
                data-testid="creation-note-add">
                Add the note
              </button>
            </div>
          )}
          <Message note={note} where="notes" />

          {creation.photos.length > 0 && (
            <ul className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4" data-testid="creation-photos">
              {creation.photos.map((p) => (
                <li key={p.image_id} data-testid={`creation-photo-${p.image_id}`}>
                  <a href={`${API_URL}${p.url}`} target="_blank" rel="noreferrer" className={`block rounded-lg ${focusRing}`}>
                    <img src={`${API_URL}${p.thumb_url}`} alt={p.caption || 'Reference photo'} loading="lazy"
                      className="aspect-square w-full rounded-lg border border-gray-300 object-cover dark:border-gray-600" />
                  </a>
                  {p.caption && <p className={`${hint} mt-1 break-words`}>{p.caption}</p>}
                  <ConfirmButton question="Remove this photo?" confirmLabel="Remove" disabled={busy} className={`${btnSecondary} mt-1 w-full`}
                    onConfirm={() => act('photos', () => postJson(`${base}/photos/${p.image_id}/delete`, {}), 'Photo removed.')}>
                    Remove
                  </ConfirmButton>
                </li>
              ))}
            </ul>
          )}
          {creation.photos.length < limits.max_photos && (
            <div className="mt-3 rounded-lg bg-gray-50 p-3 dark:bg-gray-900/40" data-testid="creation-photo-form">
              <label htmlFor="creation-photo-file" className="block text-sm font-medium">Add a photo</label>
              <p id="creation-photo-hint" className={hint}>
                PNG, JPG or WebP, at least {view.limits.min_image_px} px on each side, up to {megabytes(view.limits.max_upload_bytes)} MB.
              </p>
              <input id="creation-photo-file" key={inputKey} type="file" accept={PICTURE_ACCEPT} aria-describedby="creation-photo-hint"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)} className={`mt-1 block w-full text-sm ${focusRing}`}
                data-testid="creation-photo-file" />
              {fileIssue && <p className="mt-1 text-xs text-red-700 dark:text-red-300">{fileIssue}</p>}
              <input value={caption} onChange={(e) => setCaption(e.target.value)} maxLength={limits.caption_max}
                aria-label="Caption (optional)" placeholder="Caption (optional), e.g. the harbour at dusk" className={`${field} mt-2`}
                data-testid="creation-photo-caption" />
              {file && (
                <RightsQuestions rights={rights} onChange={setRights} group="creation-rights" testId={(w) => `creation-${w}`} />
              )}
              {file && !fileIssue && rightsIssue && <p className="mt-2 text-xs text-amber-800 dark:text-amber-300">{rightsIssue}</p>}
              <button type="button" className={`${btnSecondary} mt-2`} disabled={busy || !file || !!fileIssue || !!rightsIssue}
                onClick={uploadPhoto} data-testid="creation-photo-send">
                Upload the photo
              </button>
            </div>
          )}
          <Message note={note} where="photos" />
        </div>

        <div data-testid="creation-board">
          <StepTitle n={2} title="Mood board" status={latest ? 'done' : status(2)} />
          <p className={`${hint} mt-1`}>
            {creation.board.image_count} pictures that set the look, made from your references. You see the price first.
          </p>
          {creation.board.unavailable ? (
            <p className="mt-2 rounded-lg bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-900/20 dark:text-amber-200"
              data-testid="board-unavailable">
              {creation.board.unavailable}
            </p>
          ) : !latest && (
            <div className="mt-2">
              <button type="button" className={btnPrimary} disabled={busy || references === 0} onClick={() => checkPrice('')}
                data-testid="board-check-price">
                Check the price
              </button>
              {references === 0 && <p className={`${hint} mt-1`}>Add a note or a photo first.</p>}
              {!estimate?.feedback && estimateBox}
            </div>
          )}
          {latest && (
            <div className="mt-3" data-testid="board-latest" data-status={latest.status}>
              <p className="text-sm font-medium">
                Mood board {latest.version}
                {latest.answers_feedback && <span className="font-normal text-gray-600 dark:text-gray-300"> (asked: {latest.answers_feedback})</span>}
              </p>
              <BoardImages board={latest} />
            </div>
          )}
          <Message note={note} where="board" />
        </div>

        <div data-testid="creation-approve">
          <StepTitle n={3} title="Approve it, or redo it with feedback" status={status(3)} />
          {approved ? (
            <p className="mt-2 text-sm" data-testid="board-approved">Mood board {approved.version} is approved: it sets the look of this Universe.</p>
          ) : waiting ? (
            <div className="mt-2 space-y-3">
              <button type="button" className={btnPrimary} disabled={busy} onClick={() => approve(waiting)} data-testid="board-approve">
                Approve this mood board
              </button>
              {!creation.board.unavailable && (
                <div className="rounded-lg bg-gray-50 p-3 dark:bg-gray-900/40">
                  <label htmlFor="board-feedback" className="block text-sm font-medium">Or say what to change</label>
                  <textarea id="board-feedback" value={feedback} onChange={(e) => setFeedback(e.target.value)} rows={2}
                    maxLength={limits.feedback_max} placeholder="e.g. warmer light, fewer people, more of the sea"
                    className={`${field} mt-1`} data-testid="board-feedback" />
                  <button type="button" className={`${btnSecondary} mt-2`} disabled={busy || !feedback.trim()}
                    onClick={() => checkPrice(feedback.trim())} data-testid="board-redo-price">
                    Check the price of a redo
                  </button>
                  {estimate?.feedback && estimateBox}
                </div>
              )}
            </div>
          ) : (
            <p className={`${hint} mt-1`}>Once the mood board is made.</p>
          )}
        </div>

        <div data-testid="creation-bible">
          <StepTitle n={4} title="Characters and places" status={status(4)} />
          {bibleDone ? (
            <p className="mt-2 text-sm" data-testid="bible-done">
              Written from mood board {approved?.version}. Change them below; lock each character&apos;s look from an episode.
            </p>
          ) : approved ? (
            <div className="mt-2">
              <p className={hint}>The studio writes the characters, places and props from the approved board and your notes. Names you have already stay as they are.</p>
              <button type="button" className={`${btnPrimary} mt-2`} disabled={busy} onClick={writeBible} data-testid="bible-write">
                {busy ? 'Writing…' : 'Write the characters and places'}
              </button>
            </div>
          ) : (
            <p className={`${hint} mt-1`}>Once the mood board is approved.</p>
          )}
          {bible && (bible.skipped.length > 0 || bible.dropped.length > 0) && (
            <p className={`${hint} mt-2`} data-testid="bible-left-out">
              {bible.skipped.length > 0 && <>Kept as they were: {bible.skipped.join(', ')}. </>}
              {bible.dropped.length > 0 && <>Left out (the studio cannot make them): {bible.dropped.join(', ')}.</>}
            </p>
          )}
          <Message note={note} where="bible" />
        </div>
      </div>
    </section>
  );
}

export default UniverseCreation;
