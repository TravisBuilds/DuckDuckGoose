'use client';

import { use, useCallback, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import CastPanel from '@/components/CastPanel';
import PropsPanel from '@/components/PropsPanel';
import { ConsistencyBadge } from '@/components/ConsistencyBadge';
import { SavedVoiceBlock } from '@/components/UniverseVoices';
import { UniverseCreation } from '@/components/UniverseCreation';
import { useMe } from '@/components/StudioShell';
import { getPresetById, presets } from '@/lib/presets';
import { API_URL, ApiError, apiFetch, postJson } from '@/lib/studio-api';
import { characterBadge, countsLine, driftCount, healthLine, itemBadge, usedInLabel } from '@/lib/consistency.mjs';
import type { CastCharacter, CastVersion, CastView } from '@/lib/cast.mjs';
import type { PropItem, PropsView } from '@/lib/props.mjs';
import { groupByTemplate } from '@/lib/wizard.mjs';
import type { WorldView } from '@/lib/world.mjs';
import { formatLocalDate } from '@/lib/time';
import { ownerLabel } from '@/lib/labels';
import { btnPrimary, btnSecondary, card, chip, focusRing, input as field, link, tones, type Tone } from '@/lib/ui';

/**
 * A Universe: its header (name, one line, kind of video, owner, how many looks are locked), how it is
 * built (owner and admins), its story bible (owner and admins), its look, characters, places and
 * props (the episode panels with apiBase /api/worlds/{id}), saved voices (owner and admins), its
 * episodes, and the film placeholder. GET /api/worlds/{id} answers 404 to anyone who may not see it,
 * the same as a Universe that does not exist.
 */

const UNREACHABLE = 'The studio could not be reached. Check your connection and reload.';
const CARD_GROUPS = groupByTemplate(presets);

function castViewOf(view: WorldView): CastView {
  const l = view.limits;
  return {
    episode_id: '',
    library: { kind: 'series', id: view.world.series_id },
    series: null,
    characters: view.characters as unknown as CastCharacter[],
    shots: { uploaded: false, shots: [], plan: null, error: null },
    still_max_refs: null,
    sheet: { dry_run: false, live_mode: false, model_path: null, model_error: null, text_options: [0, 0], reference_options: [0, 0] },
    limits: {
      max_upload_bytes: l.max_upload_bytes, min_image_px: l.min_image_px, formats: l.formats,
      max_uploads_per_version: l.max_uploads_per_version, max_characters: l.max_characters, max_cast_per_shot: 0,
    },
    can_edit: view.can_edit,
  };
}

function propsViewOf(view: WorldView): PropsView {
  const l = view.limits;
  return {
    episode_id: '',
    library: { kind: 'series', id: view.world.series_id },
    series: null,
    items: view.items,
    limits: {
      max_upload_bytes: l.max_upload_bytes, min_image_px: l.min_image_px, formats: l.formats,
      max_photos_per_item: l.max_photos_per_item, max_items: l.max_items, max_props_per_shot: l.max_props_per_shot,
      name_max: l.name_max, description_max: l.description_max,
    },
    can_edit: view.can_edit,
  };
}

function StoryBible({ view, reload }: { view: WorldView; reload: () => Promise<void> }) {
  const profile = view.world.profile;
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState({ logline: '', preset_id: '', bible: '', style_notes: '' });
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ tone: 'ok' | 'problem'; text: string } | null>(null);
  const edit = () => {
    setForm({
      logline: profile.logline ?? '', preset_id: profile.preset_id ?? '', bible: profile.bible ?? '',
      style_notes: profile.style_notes ?? '',
    });
    setNote(null);
    setEditing(true);
  };
  const save = async () => {
    setBusy(true);
    setNote(null);
    try {
      await postJson(`/api/worlds/${encodeURIComponent(view.world.series_id)}/profile`, {
        logline: form.logline.trim(), preset_id: form.preset_id || null, bible: form.bible.trim(),
        style_notes: form.style_notes.trim(),
      });
      setEditing(false);
      setNote({ tone: 'ok', text: 'Saved.' });
      await reload();
    } catch (e) {
      setNote({ tone: 'problem', text: e instanceof ApiError ? e.message : UNREACHABLE });
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className={card} data-testid="universe-bible" aria-labelledby="bible-title">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="bible-title" className="text-xl font-semibold">Story bible</h2>
        {!editing && (
          <button type="button" className={btnSecondary} onClick={edit} data-testid="universe-bible-edit">Edit</button>
        )}
      </div>
      {editing ? (
        <div className="mt-3 space-y-3">
          <label className="block text-sm font-medium" htmlFor="bible-logline">In one line</label>
          <input id="bible-logline" value={form.logline} onChange={(e) => setForm({ ...form, logline: e.target.value })}
            maxLength={view.limits.logline_max} className={field} data-testid="universe-bible-logline" />
          <label className="block text-sm font-medium" htmlFor="bible-genre">Kind of video new episodes start with</label>
          <select id="bible-genre" value={form.preset_id} onChange={(e) => setForm({ ...form, preset_id: e.target.value })}
            className={field} data-testid="universe-bible-genre">
            <option value="">Decide per episode</option>
            {CARD_GROUPS.map((group) => (
              <optgroup key={group.template} label={group.label}>
                {group.presets.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </optgroup>
            ))}
          </select>
          <label className="block text-sm font-medium" htmlFor="bible-text">The story so far, the rules of this Universe</label>
          <textarea id="bible-text" value={form.bible} onChange={(e) => setForm({ ...form, bible: e.target.value })}
            maxLength={view.limits.bible_max} rows={8} className={field} data-testid="universe-bible-text" />
          <label className="block text-sm font-medium" htmlFor="bible-style">Style notes</label>
          <textarea id="bible-style" value={form.style_notes} onChange={(e) => setForm({ ...form, style_notes: e.target.value })}
            maxLength={view.limits.style_notes_max} rows={3} className={field} data-testid="universe-bible-style"
            placeholder="The look every episode keeps: colours, light, lenses, textures" />
          <div className="flex flex-wrap gap-2">
            <button type="button" className={btnPrimary} disabled={busy} onClick={save} data-testid="universe-bible-save">
              {busy ? 'Saving…' : 'Save'}
            </button>
            <button type="button" className={btnSecondary} disabled={busy} onClick={() => setEditing(false)}>Cancel</button>
          </div>
        </div>
      ) : (
        <div className="mt-2 space-y-3 text-sm text-gray-800 dark:text-gray-100">
          {profile.bible ? (
            <p className="whitespace-pre-line break-words" data-testid="universe-bible-shown">{profile.bible}</p>
          ) : (
            <p className="text-gray-600 dark:text-gray-300">
              Nothing written yet. Write the premise and the rules of this Universe, or let the studio write it from an
              approved mood board above.
            </p>
          )}
          {profile.style_notes && (
            <p className="whitespace-pre-line break-words"><span className="font-semibold">Style: </span>{profile.style_notes}</p>
          )}
        </div>
      )}
      {note && (
        <p role={note.tone === 'problem' ? 'alert' : 'status'}
          className={`mt-2 text-sm ${note.tone === 'ok' ? 'text-green-800 dark:text-green-200' : 'text-red-700 dark:text-red-300'}`}>
          {note.text}
        </p>
      )}
    </section>
  );
}

export default function UniversePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { me } = useMe();
  const [view, setView] = useState<WorldView | null>(null);
  const [problem, setProblem] = useState<{ status: number; text: string } | null>(null);
  const [coverNote, setCoverNote] = useState<string | null>(null);
  const base = `/api/worlds/${encodeURIComponent(id)}`;

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

  const lockEpisode = useMemo(
    () => view?.episodes.find((e) => !e.finished && e.can_change)?.episode_id ?? null,
    [view],
  );
  const castSource = useMemo(() => (view ? { view: castViewOf(view), reload: load, lockEpisode } : null), [view, load, lockEpisode]);
  const propsSource = useMemo(() => (view ? { view: propsViewOf(view), reload: load } : null), [view, load]);

  if (!view) {
    if (problem?.status === 404) {
      return (
        <div className="mx-auto max-w-xl px-4 py-16 text-center" data-testid="universe-not-found">
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">This Universe could not be found</h1>
          <p className="mt-2 text-gray-600 dark:text-gray-300">
            It may have been removed, or you may not have access to it. Ask its owner if you think you should.
          </p>
          <Link href="/studio" className={`${btnPrimary} mt-6`}>Back to the studio</Link>
        </div>
      );
    }
    return (
      <div className="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8">
        {problem
          ? <p role="alert" className="text-sm text-red-700 dark:text-red-300">{problem.text}</p>
          : <p className="text-sm text-gray-600 dark:text-gray-300">Loading the Universe…</p>}
      </div>
    );
  }

  const world = view.world;
  const counts = world.counts;
  const drift = driftCount(view.characters);
  const healthTone: Tone = drift > 0 ? 'bad' : counts.characters === 0 ? 'wait'
    : counts.locked_characters >= counts.characters ? 'ok' : 'warn';
  const preset = getPresetById(world.profile.preset_id ?? '');
  const owner = ownerLabel(world.owner_user_id, world.owner_name, me?.user_id ?? null);
  const byId = new Map(view.characters.map((c) => [c.character_id, c]));
  const items = new Map(view.items.map((i) => [i.prop_id, i]));
  const finished = view.episodes.filter((e) => e.finished);
  const newEpisode = `/studio/create?world=${encodeURIComponent(world.series_id)}`;

  const badge = (character: CastCharacter, version: CastVersion) => {
    const full = byId.get(character.character_id);
    const v = full?.versions.find((x) => x.version === version.version) ?? null;
    const newest = full && full.locked_versions.length > 0 ? Math.max(...full.locked_versions) : null;
    return <ConsistencyBadge badge={characterBadge(v, newest)} testId={`universe-badge-${character.slug}`} />;
  };
  const characterExtra = (character: CastCharacter) => {
    const full = byId.get(character.character_id);
    if (!full) return null;
    return (
      <>
        <p className="mt-1 text-xs text-gray-600 dark:text-gray-300" data-testid={`universe-used-${character.slug}`}>
          {usedInLabel(full.used_in)}
        </p>
        {view.can_edit && (
          <SavedVoiceBlock slotKey={`voice-${character.slug}`} label={character.name} speaker={character.name}
            saved={full.voice ?? null} saveUrl={`${base}/cast/${character.character_id}/voice`} onSaved={load}
            suggestion="a warm, lively voice in their thirties" />
        )}
      </>
    );
  };
  const itemExtra = (item: PropItem) => {
    const full = items.get(item.prop_id);
    if (!full) return null;
    return (
      <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-gray-600 dark:text-gray-300">
        <ConsistencyBadge badge={itemBadge(full)} testId={`universe-item-badge-${item.slug}`} />
        <span data-testid={`universe-item-used-${item.slug}`}>{usedInLabel(full.used_in)}</span>
      </p>
    );
  };
  const chooseCover = async (imageId: string) => {
    setCoverNote(null);
    try {
      await postJson(`${base}/profile`, { cover_image_id: imageId });
      setCoverNote('Cover changed.');
      await load();
    } catch (e) {
      setCoverNote(e instanceof ApiError ? e.message : UNREACHABLE);
    }
  };

  return (
    <div className="mx-auto max-w-5xl space-y-6 px-4 py-8 sm:px-6 lg:px-8" data-testid="universe-page">
      <nav aria-label="Breadcrumb" className="text-sm text-gray-600 dark:text-gray-300">
        <Link href="/studio" className={link}>Universes</Link>
        <span aria-hidden="true"> › </span>
        <span className="break-words" aria-current="page">{world.name}</span>
      </nav>

      <header className={card} data-testid="universe-header">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start">
          {world.cover && (
            <img src={`${API_URL}${world.cover.thumb_url}`} alt="" className="h-40 w-full rounded-lg object-cover sm:h-32 sm:w-48" />
          )}
          <div className="min-w-0 flex-1">
            <h1 className="break-words text-2xl font-bold text-gray-900 dark:text-white" data-testid="universe-name">{world.name}</h1>
            {world.profile.logline && (
              <p className="mt-1 break-words text-gray-700 dark:text-gray-200" data-testid="universe-logline">{world.profile.logline}</p>
            )}
            <p className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-sm text-gray-600 dark:text-gray-300">
              {preset && <span data-testid="universe-genre">{preset.name}</span>}
              <span data-testid="universe-owner">{owner === 'You' ? 'By you' : `By ${owner}`}</span>
              <span>{countsLine(counts)}</span>
            </p>
            <p className="mt-2">
              <span className={`${chip} ${tones[healthTone]}`} data-testid="universe-health">{healthLine(counts, drift)}</span>
            </p>
          </div>
          <Link href={newEpisode} className={`${btnPrimary} w-full sm:w-auto`} data-testid="universe-new-episode">
            New episode in this Universe
          </Link>
        </div>
      </header>

      {view.can_edit && view.creation && <UniverseCreation view={view} reload={load} />}

      {view.can_edit && <StoryBible view={view} reload={load} />}

      {view.look && (
        <section className={card} data-testid="universe-look" aria-labelledby="look-title">
          <h2 id="look-title" className="text-xl font-semibold">Look</h2>
          <p className="text-sm text-gray-700 dark:text-gray-300">The approved mood board: every episode in this Universe keeps this look.</p>
          <ul className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
            {view.look.images.map((image) => (
              <li key={image.image_id}>
                <a href={`${API_URL}${image.url}`} target="_blank" rel="noreferrer" className={`block rounded-lg ${focusRing}`}>
                  <img src={`${API_URL}${image.thumb_url}`} alt={`Mood board picture ${image.position + 1}`} loading="lazy"
                    className="aspect-square w-full rounded-lg border border-gray-300 object-cover dark:border-gray-600" />
                </a>
                {view.can_edit && (
                  world.profile.cover_image_id === image.image_id
                    ? <span className={`${chip} ${tones.ok} mt-1`}>Cover</span>
                    : (
                      <button type="button" className={`${btnSecondary} mt-1 w-full`} onClick={() => chooseCover(image.image_id)}>
                        Use as cover
                      </button>
                    )
                )}
              </li>
            ))}
          </ul>
          {coverNote && <p role="status" className="mt-2 text-sm">{coverNote}</p>}
        </section>
      )}

      <div id="characters">
        {castSource && (
          <CastPanel apiBase={base} universe={castSource} badge={badge} extra={characterExtra} />
        )}
      </div>

      {view.can_edit && (
        <section className={card} data-testid="universe-narrator" aria-labelledby="narrator-title">
          <h2 id="narrator-title" className="text-xl font-semibold">Narrator</h2>
          <p className="text-sm text-gray-700 dark:text-gray-300">
            The voice that reads the narration. New episodes in this Universe start with it; you can pick another per episode.
          </p>
          <SavedVoiceBlock slotKey="narrator" label="Narrator" speaker={null} saved={world.profile.narrator_voice ?? null}
            saveUrl={`${base}/narrator-voice`} onSaved={load} suggestion="a calm storyteller with a warm, low voice" />
        </section>
      )}

      <div id="places">
        {propsSource && <PropsPanel apiBase={base} universe={propsSource} extra={itemExtra} />}
      </div>

      <section className={card} data-testid="universe-episodes" aria-labelledby="episodes-title">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="episodes-title" className="text-xl font-semibold">Episodes</h2>
          <Link href={newEpisode} className={btnSecondary}>New episode</Link>
        </div>
        {view.episodes.length === 0 ? (
          <p className="mt-2 text-sm text-gray-700 dark:text-gray-300" data-testid="universe-episodes-empty">
            No episodes yet. Start one: its characters, places and look come from this Universe.
          </p>
        ) : (
          <ul className="mt-3 divide-y divide-gray-200 dark:divide-gray-700">
            {view.episodes.map((e) => {
              const by = ownerLabel(e.owner_user_id, e.owner_name, me?.user_id ?? null);
              return (
                <li key={e.episode_id} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-sm"
                  data-testid={`universe-episode-${e.episode_id}`}>
                  <Link href={`/studio/episodes/${encodeURIComponent(e.episode_id)}`} className={`font-mono font-semibold ${link}`}>
                    {e.episode_id}
                  </Link>
                  <span className={`${chip} ${e.finished ? tones.ok : tones.todo}`}>{e.finished ? 'Finished' : 'In progress'}</span>
                  <span className="text-gray-600 dark:text-gray-300">{by === 'You' ? 'By you' : `By ${by}`}</span>
                  {e.created_at && <span className="text-gray-600 dark:text-gray-300">{formatLocalDate(e.created_at)}</span>}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section className={card} data-testid="universe-film" aria-labelledby="film-title">
        <div className="flex flex-wrap items-center gap-2">
          <h2 id="film-title" className="text-xl font-semibold">Join episodes into a film</h2>
          <span className={`${chip} ${tones.wait}`} data-testid="universe-film-soon">Coming soon</span>
        </div>
        <p className="mt-1 text-sm text-gray-700 dark:text-gray-300">
          Pick finished episodes and their order, and the studio joins them into one film with the same look.
        </p>
        {finished.length === 0 ? (
          <p className="mt-2 text-sm text-gray-600 dark:text-gray-300">No finished episodes yet.</p>
        ) : (
          <ul className="mt-2 space-y-1 text-sm" data-testid="universe-film-episodes">
            {finished.map((e) => (
              <li key={e.episode_id}>
                <label className="inline-flex items-center gap-2 text-gray-600 dark:text-gray-300">
                  <input type="checkbox" disabled /> <span className="font-mono">{e.episode_id}</span>
                </label>
              </li>
            ))}
          </ul>
        )}
        <button type="button" disabled className={`${btnSecondary} mt-3`} data-testid="universe-film-button">Join into a film</button>
      </section>
    </div>
  );
}
