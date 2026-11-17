'use client';

import { use, useCallback, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import CastPanel from '@/components/CastPanel';
import PropsPanel from '@/components/PropsPanel';
import { ConsistencyBadge } from '@/components/ConsistencyBadge';
import { SavedVoiceBlock } from '@/components/UniverseVoices';
import { useMe } from '@/components/StudioShell';
import {
  Avatar, GlassPanel, HeroBand, Icon, IconCircle, JourneyPath, NextStepPill, Polaroid, Seal,
} from '@/components/playful';
import type { JourneyStop } from '@/components/playful/JourneyPath';
import { getPresetById, presets } from '@/lib/presets';
import { API_URL, ApiError, apiFetch, postJson } from '@/lib/studio-api';
import { characterBadge, countsLine, driftCount, healthLine, itemBadge, usedInLabel } from '@/lib/consistency.mjs';
import { castColours, castColourAt } from '@/lib/castColour.mjs';
import { creationJourney, sealFor, toneChips } from '@/lib/journey.mjs';
import type { CastCharacter, CastVersion, CastView } from '@/lib/cast.mjs';
import { itemPhotos } from '@/lib/props.mjs';
import type { PropItem, PropsView } from '@/lib/props.mjs';
import { groupByTemplate } from '@/lib/wizard.mjs';
import type { WorldView } from '@/lib/world.mjs';
import { formatLocalDate } from '@/lib/time';
import { ownerLabel } from '@/lib/labels';
import { btnPrimary, btnSecondary, input as field, tones, type Tone } from '@/lib/ui';

/**
 * A Universe (DESIGN-TARGETS-v2 §6.1): a scene banner with its name, one line, kind of video and
 * owner beside the consistency panel (seal, what is locked, New episode); how it was made, as the
 * five-step path (owner and admins); then its characters, places and props (the episode panels with
 * apiBase /api/worlds/{id}), story bible (owner and admins), look, narrator voice (owner and admins),
 * episodes (the ones the reader may see) and the film placeholder. GET /api/worlds/{id} answers 404
 * to anyone who may not see it, the same as a Universe that does not exist.
 */

const UNREACHABLE = 'The studio could not be reached. Check your connection and reload.';
const CARD_GROUPS = groupByTemplate(presets);
const TABS = [
  { key: 'overview', label: 'Overview' },
  { key: 'characters', label: 'Characters' },
  { key: 'places', label: 'Places & props' },
  { key: 'bible', label: 'Bible', editors: true },
  { key: 'look', label: 'Mood board' },
  { key: 'voices', label: 'Voices', editors: true },
  { key: 'episodes', label: 'Episodes' },
];

function castViewOf(view: WorldView): CastView {
  const l = view.limits;
  return {
    episode_id: '',
    library: { kind: 'series', id: view.world.series_id },
    series: null,
    characters: view.characters as unknown as CastCharacter[],
    shots: { uploaded: false, shots: [], plan: null, error: null },
    prompts_hidden: false,
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

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
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
  const label = 'block text-[13px] font-extrabold';
  const tone = toneChips(profile.style_notes);
  return (
    <section className="pf-card scroll-mt-24 p-5 sm:p-6" id="bible" data-testid="universe-bible" aria-labelledby="bible-title">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="bible-title" className="text-[22px]">Story bible</h2>
        {!editing && (
          <button type="button" className={`${btnSecondary} pf-btn-sm`} onClick={edit} data-testid="universe-bible-edit">Edit</button>
        )}
      </div>
      {editing ? (
        <div className="mt-3 space-y-3">
          <label className={label} htmlFor="bible-logline">In one line</label>
          <input id="bible-logline" value={form.logline} onChange={(e) => setForm({ ...form, logline: e.target.value })}
            maxLength={view.limits.logline_max} className={field} data-testid="universe-bible-logline" />
          <label className={label} htmlFor="bible-genre">Kind of video new episodes start with</label>
          <select id="bible-genre" value={form.preset_id} onChange={(e) => setForm({ ...form, preset_id: e.target.value })}
            className={field} data-testid="universe-bible-genre">
            <option value="">Decide per episode</option>
            {CARD_GROUPS.map((group) => (
              <optgroup key={group.template} label={group.label}>
                {group.presets.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </optgroup>
            ))}
          </select>
          <label className={label} htmlFor="bible-text">The story so far, the rules of this Universe</label>
          <textarea id="bible-text" value={form.bible} onChange={(e) => setForm({ ...form, bible: e.target.value })}
            maxLength={view.limits.bible_max} rows={8} className={field} data-testid="universe-bible-text" />
          <label className={label} htmlFor="bible-style">Style notes</label>
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
        <div className="mt-3 space-y-3 text-[14px]">
          {profile.logline && (
            <div>
              <h3 className="font-body text-[12px] font-extrabold uppercase tracking-[0.06em] text-ink-soft">Premise</h3>
              <p className="mt-0.5 break-words">{profile.logline}</p>
            </div>
          )}
          <div>
            <h3 className="font-body text-[12px] font-extrabold uppercase tracking-[0.06em] text-ink-soft">How this Universe works</h3>
            {profile.bible ? (
              <p className="mt-0.5 whitespace-pre-line break-words" data-testid="universe-bible-shown">{profile.bible}</p>
            ) : (
              <p className="mt-0.5 text-ink-soft">
                Nothing written yet. Write the premise and the rules of this Universe, or let the studio write it from an
                approved mood board.
              </p>
            )}
          </div>
          {profile.style_notes && (
            <div>
              <h3 className="font-body text-[12px] font-extrabold uppercase tracking-[0.06em] text-ink-soft">Style</h3>
              <p className="mt-0.5 whitespace-pre-line break-words"><span className="sr-only">Style: </span>{profile.style_notes}</p>
              {tone.length > 1 && (
                <ul className="mt-2 flex flex-wrap gap-1.5" aria-label="Tone">
                  {tone.map((t) => <li key={t} className="pf-chip bg-mustard-soft text-mustard-ink">{t}</li>)}
                </ul>
              )}
            </div>
          )}
        </div>
      )}
      {note && (
        <p role={note.tone === 'problem' ? 'alert' : 'status'}
          className={`mt-2 text-[13px] font-bold ${note.tone === 'ok' ? 'text-green-ink' : 'text-coral-ink'}`}>
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
  const [tab, setTab] = useState('overview');
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
          <IconCircle icon="search" size={80} className="mx-auto" />
          <h1 className="mt-4 text-[28px]">This Universe could not be found</h1>
          <p className="mt-2 text-ink-soft">
            It may have been removed, or you may not have access to it. Ask its owner if you think you should.
          </p>
          <Link href="/studio" className={`${btnPrimary} mt-6`}>Back to the studio</Link>
        </div>
      );
    }
    return (
      <div className="mx-auto max-w-[1240px] px-4 py-8 sm:px-8">
        {problem
          ? <p role="alert" className="text-[13.5px] font-bold text-coral-ink">{problem.text}</p>
          : <p className="text-[13.5px] text-ink-soft">Loading the Universe…</p>}
      </div>
    );
  }

  const world = view.world;
  const counts = world.counts;
  const drift = driftCount(view.characters);
  const healthTone: Tone = drift > 0 ? 'bad' : counts.characters === 0 ? 'wait'
    : counts.locked_characters >= counts.characters ? 'ok' : 'warn';
  const seal = sealFor(counts, drift);
  const preset = getPresetById(world.profile.preset_id ?? '');
  const owner = ownerLabel(world.owner_user_id, world.owner_name, me?.user_id ?? null);
  const byId = new Map(view.characters.map((c) => [c.character_id, c]));
  const items = new Map(view.items.map((i) => [i.prop_id, i]));
  const finished = view.episodes.filter((e) => e.finished);
  const newEpisode = `/studio/create?world=${encodeURIComponent(world.series_id)}`;
  const colours = castColours(view.characters);
  const cast = view.characters.slice(0, 5);
  const withPhotos = view.items.filter((i) => itemPhotos(i).length > 0).length;
  const voices = view.characters.filter((c) => c.voice).length;
  const journey = view.can_edit && view.creation ? creationJourney(view) : null;
  const stepHref = (key: string) => `/studio/universes/${encodeURIComponent(world.series_id)}/create?step=${key}`;
  const unfinished = journey?.steps.find((s) => s.state !== 'done') ?? null;
  const stops: JourneyStop[] = journey ? [
    ...journey.steps.map((s) => ({
      key: s.key, name: s.name, note: s.note, state: s.state, paid: s.key === 'moodboard', href: stepHref(s.key),
    })),
    {
      key: 'episodes', name: 'Episodes', note: view.episodes.length ? `${plural(view.episodes.length, 'episode')} made` : 'None yet',
      state: unfinished ? 'todo' : view.episodes.length ? 'done' : 'now', href: unfinished ? null : newEpisode,
    },
  ] : [];

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
        <p className="mt-1 text-[12px] font-bold text-ink-soft" data-testid={`universe-used-${character.slug}`}>
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
      <p className="mt-2 flex flex-wrap items-center gap-2 text-[12px] font-bold text-ink-soft">
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
  const row = 'flex items-center justify-between gap-3 py-2';

  return (
    <div className="mx-auto max-w-[1240px] px-4 pb-12 pt-3 sm:px-8 sm:pt-6" data-testid="universe-page">
      <section id="overview" className="scroll-mt-24" data-testid="universe-header" aria-labelledby="universe-title">
        <HeroBand className="grid items-start gap-6 px-5 pb-8 pt-6 sm:px-10 sm:pt-8 lg:min-h-[360px] lg:grid-cols-[1fr_340px] lg:pb-10">
          <div className="relative z-10 min-w-0">
            <nav aria-label="Breadcrumb" className="text-[13px] font-extrabold text-ink">
              <Link href="/studio" className="text-blue-deep">Universes</Link>
              <span aria-hidden="true"> › </span>
              <span className="break-words" aria-current="page">{world.name}</span>
            </nav>
            <h1 id="universe-title" className="mt-2 break-words text-[36px] leading-[1.02] sm:text-[54px]" data-testid="universe-name">
              {world.name}
            </h1>
            {world.profile.logline && (
              <p className="mt-2 max-w-[46ch] break-words text-[16px] sm:text-[17px]" data-testid="universe-logline">{world.profile.logline}</p>
            )}
            <div className="mt-3.5 flex flex-wrap gap-2">
              {preset && <span className="pf-chip bg-white text-ink shadow-card" data-testid="universe-genre">{preset.name}</span>}
              <span className="pf-chip bg-white text-ink shadow-card" data-testid="universe-owner">{owner === 'You' ? 'By you' : `By ${owner}`}</span>
              <span className="pf-chip bg-white text-ink shadow-card">{countsLine(counts)}</span>
            </div>
            {cast.length > 0 && (
              <div aria-hidden="true" className="mt-5 flex items-center -space-x-2.5">
                {cast.map((c, i) => (
                  <span key={c.character_id} className="rounded-full border-[3px] border-sky">
                    <Avatar name={c.name} hue={(colours[c.slug] ?? castColourAt(i)).colour} size={46} />
                  </span>
                ))}
                {view.characters.length > cast.length && (
                  <span className="grid size-[52px] place-items-center rounded-full border-[3px] border-sky bg-white font-display text-[13px] font-bold text-ink">
                    +{view.characters.length - cast.length}
                  </span>
                )}
              </div>
            )}
          </div>

          <GlassPanel as="aside" className="relative z-10 p-5" aria-labelledby="consistency-title">
            <div className="flex items-center gap-3.5">
              <Seal state={seal.state} top={seal.top} bottom={seal.bottom} size={64} />
              <div className="min-w-0">
                <h2 id="consistency-title" className="text-[20px]">Consistency</h2>
                <p className="text-[12.5px] text-ink-soft">What every episode will reuse, exactly.</p>
              </div>
            </div>
            <p className="mt-3">
              <span className={`pf-chip ${tones[healthTone]}`} data-testid="universe-health">{healthLine(counts, drift)}</span>
            </p>
            <ul className="mt-2 divide-y-2 divide-dashed divide-line text-[13.5px] font-bold">
              <li className={row}>
                <span>Characters</span>
                <span className={counts.characters > 0 && counts.locked_characters >= counts.characters ? 'text-green-ink' : 'text-mustard-ink'}>
                  {counts.locked_characters} of {counts.characters} locked
                </span>
              </li>
              <li className={row}>
                <span>Places &amp; props</span>
                <span className={view.items.length > 0 && withPhotos === view.items.length ? 'text-green-ink' : 'text-ink-soft'}>
                  {withPhotos} of {view.items.length} with photos
                </span>
              </li>
              {view.can_edit && (
                <li className={row}>
                  <span>Voices</span>
                  <span className={counts.characters > 0 && voices >= counts.characters ? 'text-green-ink' : 'text-ink-soft'}>
                    {voices} of {counts.characters} saved
                  </span>
                </li>
              )}
              <li className={row}>
                <span>Look changed since lock</span>
                <span className={drift > 0 ? 'text-coral-ink' : counts.locked_characters > 0 ? 'text-green-ink' : 'text-ink-soft'}
                  data-testid="universe-drift">
                  {drift > 0 ? plural(drift, 'character') : 'None'}
                </span>
              </li>
            </ul>
            {view.can_edit ? (
              <Link href={newEpisode} className="pf-btn pf-btn-primary mt-3 w-full" data-testid="universe-new-episode">
                New episode in this Universe
              </Link>
            ) : (
              <p className="mt-3 rounded-inner bg-page p-3 text-[13px]" data-testid="universe-new-episode-note">
                Only the owner of this Universe, or an admin, can start episodes in it.
              </p>
            )}
            <p className="mt-2 text-center text-[12px] text-ink-soft">An episode starts once every character in it is locked.</p>
          </GlassPanel>

        </HeroBand>
      </section>

      {journey && (
        <section className="pf-card mt-5 p-5 sm:p-6" aria-labelledby="journey-title" data-testid="universe-journey">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 id="journey-title" className="text-[22px]">How {world.name} was made</h2>
              <p className="text-[13.5px] text-ink-soft">Five steps, done once. Open any step to look again or change it.</p>
            </div>
            <NextStepPill className="w-full sm:w-auto" testId="universe-next"
              text={unfinished ? `Next: ${unfinished.name.toLowerCase()}` : 'Ready: make an episode'}
              tone={unfinished ? 'needs' : 'ok'} href={unfinished ? stepHref(unfinished.key) : newEpisode} />
          </div>
          <JourneyPath stops={stops} current={unfinished?.key ?? 'episodes'} label={`How ${world.name} was made`}
            testId="universe-journey-path" className="mt-3" />
        </section>
      )}

      <nav aria-label="On this page" className="mt-5 flex gap-2 overflow-x-auto pb-1">
        {TABS.filter((t) => !t.editors || view.can_edit).map((t) => (
          <a key={t.key} href={`#${t.key}`} onClick={() => setTab(t.key)} aria-current={tab === t.key ? 'true' : undefined}
            className={`pf-tab shrink-0 ${tab === t.key ? 'is-on' : 'bg-white shadow-card'}`}>
            {t.label}
          </a>
        ))}
      </nav>

      <div className="mt-5 grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_380px]">
        <div className="min-w-0 space-y-10">
          <div id="characters" className="scroll-mt-24">
            {castSource && (
              <CastPanel apiBase={base} universe={castSource} badge={badge} extra={characterExtra} />
            )}
          </div>
          <div id="places" className="scroll-mt-24">
            {propsSource && <PropsPanel apiBase={base} universe={propsSource} extra={itemExtra} />}
          </div>
        </div>

        <div className="min-w-0 space-y-6">
          {view.can_edit && <StoryBible view={view} reload={load} />}

          {view.look ? (
            <section className="pf-card scroll-mt-24 p-5" id="look" data-testid="universe-look" aria-labelledby="look-title">
              <h2 id="look-title" className="text-[22px]">Mood board</h2>
              <p className="text-[13.5px] text-ink-soft">The approved mood board: every episode in this Universe keeps this look.</p>
              <div className="pf-board mt-3">
                <ul className="grid grid-cols-2 gap-5">
                  {view.look.images.map((image, i) => (
                    <Polaroid key={image.image_id} index={i} picture={{
                      key: image.image_id, src: `${API_URL}${image.thumb_url}`, href: `${API_URL}${image.url}`,
                      alt: `Mood board picture ${image.position + 1}`,
                    }}>
                      {view.can_edit && (
                        world.profile.cover_image_id === image.image_id
                          ? <span className="pf-chip pf-chip-lock mt-2">Cover</span>
                          : (
                            <button type="button" className="pf-btn pf-btn-quiet pf-btn-sm mt-2 w-full" onClick={() => chooseCover(image.image_id)}>
                              Use as cover
                            </button>
                          )
                      )}
                    </Polaroid>
                  ))}
                </ul>
              </div>
              {coverNote && <p role="status" className="mt-2 text-[13px] font-bold">{coverNote}</p>}
            </section>
          ) : (
            <section className="pf-card scroll-mt-24 p-5" id="look" aria-labelledby="look-title">
              <h2 id="look-title" className="text-[22px]">Mood board</h2>
              <p className="text-[13.5px] text-ink-soft">
                No approved mood board yet.{' '}
                {journey && <Link href={stepHref('moodboard')} className="font-extrabold text-blue-deep underline">Make one</Link>}
              </p>
            </section>
          )}

          {view.can_edit && (
            <section className="pf-card scroll-mt-24 p-5" id="voices" data-testid="universe-narrator" aria-labelledby="narrator-title">
              <h2 id="narrator-title" className="text-[22px]">Narrator</h2>
              <p className="text-[13.5px] text-ink-soft">
                The voice that reads the narration. New episodes in this Universe start with it; you can pick another per episode.
                Each character&apos;s voice is on their card.
              </p>
              <SavedVoiceBlock slotKey="narrator" label="Narrator" speaker={null} saved={world.profile.narrator_voice ?? null}
                saveUrl={`${base}/narrator-voice`} onSaved={load} suggestion="a calm storyteller with a warm, low voice" />
            </section>
          )}

          <section className="pf-card scroll-mt-24 p-5" id="episodes" data-testid="universe-episodes" aria-labelledby="episodes-title">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 id="episodes-title" className="text-[22px]">Episodes</h2>
              {view.can_edit && <Link href={newEpisode} className={`${btnSecondary} pf-btn-sm`}>New episode</Link>}
            </div>
            {!view.can_edit && (
              <p className="mt-1 text-[13.5px] text-ink-soft" data-testid="universe-episodes-note">
                You see the episodes you made in this Universe and the shared ones.
              </p>
            )}
            {view.episodes.length === 0 ? (
              <p className="mt-2 text-[13.5px] text-ink-soft" data-testid="universe-episodes-empty">
                No episodes yet. Start one: its characters, places and look come from this Universe.
              </p>
            ) : (
              <ul className="mt-3 space-y-2">
                {view.episodes.map((e) => {
                  const by = ownerLabel(e.owner_user_id, e.owner_name, me?.user_id ?? null);
                  return (
                    <li key={e.episode_id} className="relative flex items-center gap-3 rounded-[20px] bg-page py-2 pl-2 pr-2.5 text-[13px]"
                      data-testid={`universe-episode-${e.episode_id}`}>
                      <span aria-hidden="true" className="grid size-11 flex-none place-items-center rounded-full bg-gradient-to-br from-blue-soft to-aqua text-blue-ink">
                        <Icon name="film" size={20} />
                      </span>
                      <div className="min-w-0 flex-1">
                        <Link href={`/studio/episodes/${encodeURIComponent(e.episode_id)}`}
                          className="font-mono font-extrabold text-blue-deep underline after:absolute after:inset-0 after:rounded-[20px] after:content-['']">
                          {e.episode_id}
                        </Link>
                        <p className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1">
                          <span className={`pf-chip ${e.finished ? tones.ok : tones.info}`}>{e.finished ? 'Finished' : 'In progress'}</span>
                          <span className="text-ink-soft">{by === 'You' ? 'By you' : `By ${by}`}</span>
                          {e.created_at && <span className="text-ink-soft">{formatLocalDate(e.created_at)}</span>}
                        </p>
                      </div>
                      <span aria-hidden="true" className="pf-round is-solid !size-10"><Icon name="triangle" size={16} /></span>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>

          <section className="pf-card p-5" data-testid="universe-film" aria-labelledby="film-title">
            <div className="flex flex-wrap items-center gap-2">
              <h2 id="film-title" className="text-[20px]">Join episodes into a film</h2>
              <span className={`pf-chip ${tones.wait}`} data-testid="universe-film-soon">Coming soon</span>
            </div>
            <p className="mt-1 text-[13.5px] text-ink-soft">
              Pick finished episodes and their order, and the studio joins them into one film with the same look.
            </p>
            {finished.length === 0 ? (
              <p className="mt-2 text-[13px] text-ink-soft">No finished episodes yet.</p>
            ) : (
              <ul className="mt-2 space-y-1 text-[13px]" data-testid="universe-film-episodes">
                {finished.map((e) => (
                  <li key={e.episode_id}>
                    <label className="inline-flex items-center gap-2 text-ink-soft">
                      <input type="checkbox" disabled /> <span className="font-mono">{e.episode_id}</span>
                    </label>
                  </li>
                ))}
              </ul>
            )}
            <button type="button" disabled className={`${btnSecondary} mt-3`} data-testid="universe-film-button">Join into a film</button>
          </section>
        </div>
      </div>
    </div>
  );
}
