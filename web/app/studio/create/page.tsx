'use client';

import React, { Suspense, useState } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { formatUsd } from '@/lib/money';
import { ApiError, apiFetch, postJson } from '@/lib/studio-api';
import { VoicePreview, VoiceSlotPicker, emptyVoice, type VoiceState } from '@/components/VoiceSlotPicker';
import { presets, getPresetById } from '@/lib/presets';
import { chip, tones } from '@/lib/ui';
import { autoMapCast, castMatchText } from '@/lib/consistency.mjs';
import { castColours, speakerColours } from '@/lib/castColour.mjs';
import { CastDot, Dots, HeroBand, JourneyPath, SpeakerChip } from '@/components/playful';
import { ChosenPictureSize } from '@/components/PictureSize';
import type { JourneyStop } from '@/components/playful/JourneyPath';
import { type EpisodeUniverse, type StartRequest, type WorldView } from '@/lib/world.mjs';
import {
  FREEFORM_DEFAULT_LENGTH,
  FREEFORM_LENGTH_RANGE,
  TEMPLATE_LABELS,
  TEXT_CARD_MAX_CHARS,
  applyOnScreenText,
  applyVoiceChoices,
  briefFieldLabel,
  composeIdea,
  formatAspect,
  groupByTemplate,
  hasVoiceLines,
  lengthOptions,
  onScreenTextDefaults,
  onScreenTextProblem,
  savedVoiceChoices,
  slotToken,
  soundLabel,
  voiceBlockMessage,
  type OnScreenTextChoices,
  type SavedVoicePick,
  type VoiceChoices,
  type VoicePick,
  type VoiceSlot,
} from '@/lib/wizard.mjs';
import {
  PICTURE_ACCEPT,
  castImportBody,
  castRowsProblem,
  draftCastRows,
  emptyRights,
  kitWithCast,
  pictureProblem,
  rightsFields,
  rightsProblem,
  shotsWithCast,
  type CastCharacter,
  type DraftCastEntry,
  type DraftCastRow,
  type ImportedCharacter,
  type RightsAnswers,
  type SeriesInfo,
} from '@/lib/cast.mjs';

/** The card a draft follows (api/main.py DRAFT_PRESET_FIELDS). */
interface DraftPreset {
  id: string;
  name: string;
  template: string;
  story_structure: string;
  aspect: string;
  vo_mode: string;
  music_mode: string;
  music: string;
  length_s: [number, number];
  default_length_s: number;
  on_screen_text?: string[];
}

interface DraftResponse {
  episode_id: string;
  beatmap: string;
  shots_json: { shots: any[] };
  refs_json: any;
  prompt_kit_json: any;
  audio_plan_json: any;
  cost_estimate?: {
    total_usd_micros: number;
    per_shot: Record<string, { stills_usd_micros: number; clip_usd_micros: number | null }>;
  };
  warnings?: string[];
  archetype_id?: string | null;
  preset?: DraftPreset | null;
  target_duration?: number | null;
  voice_slots?: VoiceSlot[];
  default_voice_available?: boolean;
  title?: string | null;
  cast?: DraftCastEntry[];
}

type WizardStep = 'archetype' | 'idea' | 'review' | 'loading';

const CARD_GROUPS = groupByTemplate(presets);

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** Voice state for a new draft: slots that are still there keep their description and voice. */
function voicesForDraft(slots: VoiceSlot[], previous: Record<string, VoiceState>): Record<string, VoiceState> {
  return Object.fromEntries(
    slots.map(slot => [slot.key, { ...(previous[slot.key] ?? emptyVoice()), busy: false, error: null }]),
  );
}

/** A draft's characters matched to the Universe's by name: a match is the same character, reused. */
function withUniverseCast(rows: DraftCastRow[], characters: CastCharacter[]): DraftCastRow[] {
  const mapping = autoMapCast(rows, characters);
  return rows.map(row => ({ ...row, seriesSlug: mapping[row.code] ?? '' }));
}

/** A slot that starts with the voice saved in the Universe; the creator may pick another for this episode. */
function SavedVoiceSlot({ slot, pick, onChange }: { slot: VoiceSlot; pick: SavedVoicePick; onChange: () => void }) {
  const token = slotToken(slot.key);
  return (
    <div className="border-t border-gray-200 pt-4 first:border-t-0 first:pt-0" data-testid={`voice-saved-${token}`}>
      <p className="text-sm font-semibold">{slot.label}</p>
      <p className="text-xs text-gray-500 mb-2">{slot.lines} voice-over line{slot.lines === 1 ? '' : 's'}</p>
      <div className="flex flex-wrap items-center gap-3 bg-gray-50 rounded-lg p-3" data-voice-id={pick.voice_id}>
        <span className="text-sm" data-testid={`voice-saved-name-${token}`}>
          Saved voice: <strong>{pick.name || 'unnamed voice'}</strong>
        </span>
        <VoicePreview pick={pick} token={`saved-${token}`} />
        <button
          type="button"
          onClick={onChange}
          className="px-3 py-1.5 border border-gray-300 rounded-lg text-sm font-semibold hover:bg-gray-50"
          data-testid={`voice-saved-change-${token}`}
        >
          Use a different voice for this episode
        </button>
      </div>
    </div>
  );
}

function CreateVideoWizard() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const preselectedPreset = getPresetById(searchParams?.get('archetype') ?? '');
  const worldId = searchParams?.get('world') ?? '';

  const [step, setStep] = useState<WizardStep>(preselectedPreset ? 'idea' : 'archetype');
  const [selectedArchetype, setSelectedArchetype] = useState<string | null>(preselectedPreset?.id ?? null);
  const [templateFilter, setTemplateFilter] = useState<string | null>(null);
  const [idea, setIdea] = useState('');
  const [briefAnswers, setBriefAnswers] = useState<Record<string, string>>({});
  const [targetDuration, setTargetDuration] = useState(preselectedPreset?.defaultLength ?? FREEFORM_DEFAULT_LENGTH);
  const [stylePreset, setStylePreset] = useState('');
  const [draft, setDraft] = useState<DraftResponse | null>(null);
  const [submittedIdea, setSubmittedIdea] = useState('');
  const [voices, setVoices] = useState<Record<string, VoiceState>>({});
  const [textOnScreen, setTextOnScreen] = useState<OnScreenTextChoices | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showLiveConfirm, setShowLiveConfirm] = useState(false);
  // off: the video starts at once; on: it waits on its page while its owner picks each shot's first frame
  const [pickFrames, setPickFrames] = useState(false);
  const [liveAvailable, setLiveAvailable] = useState(false);
  const [castRows, setCastRows] = useState<DraftCastRow[]>([]);
  const [castPictures, setCastPictures] = useState<Record<string, File | null>>({});
  const [castRights, setCastRights] = useState<RightsAnswers>(emptyRights());
  const [seriesList, setSeriesList] = useState<SeriesInfo[] | null>(null);
  const [seriesId, setSeriesId] = useState('');
  const [seriesCast, setSeriesCast] = useState<CastCharacter[]>([]);
  const [universe, setUniverse] = useState<WorldView | null>(null);
  const [universeIssue, setUniverseIssue] = useState<string | null>(null);
  const [ownVoice, setOwnVoice] = useState<Record<string, boolean>>({});

  const selectedPreset = selectedArchetype ? getPresetById(selectedArchetype) ?? null : null;
  const lengthRange = selectedPreset?.lengthRange ?? FREEFORM_LENGTH_RANGE;
  const recommendedLength = selectedPreset?.defaultLength ?? FREEFORM_DEFAULT_LENGTH;
  const voiceSlots = draft?.voice_slots ?? [];
  const savedVoices: Record<string, SavedVoicePick> = universe ? savedVoiceChoices(voiceSlots, universe) : {};
  const voiceChoices: VoiceChoices = {
    ...Object.fromEntries(Object.entries(voices).map(([key, state]) => [key, state.choice])),
    ...Object.fromEntries(Object.entries(savedVoices).filter(([key]) => !ownVoice[key])),
  };
  const voiceBlock = draft ? voiceBlockMessage(voiceSlots, voiceChoices, !!draft.default_voice_available) : null;
  const textBlock = draft ? onScreenTextProblem(textOnScreen) : null;
  const chosenPictures = Object.values(castPictures).filter((file): file is File => !!file);
  const castBlock = draft
    ? castRowsProblem(castRows)
      ?? chosenPictures.map(file => pictureProblem(file, null)).find(Boolean)
      ?? (chosenPictures.length > 0 ? rightsProblem(castRights) : null)
    : null;
  const startBlock = voiceBlock ?? textBlock ?? castBlock;
  const draftSeconds = (draft?.shots_json.shots ?? []).reduce((total, shot) => total + (Number(shot.duration) || 0), 0);

  // Check if live mode is available on mount
  React.useEffect(() => {
    const checkLiveAvailability = async () => {
      try {
        const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
        const response = await fetch(`${apiUrl}/api/health`, { credentials: 'include' });
        if (response.ok) {
          const data = await response.json();
          setLiveAvailable(data.live_available === true);
        }
      } catch (e) {
        // If health check fails, assume live not available
        setLiveAvailable(false);
      }
    };
    checkLiveAvailability();
  }, []);

  // A new episode in a Universe (?world=): its genre is the default style and its characters are reused
  React.useEffect(() => {
    if (!worldId) return;
    let cancelled = false;
    apiFetch<WorldView>(`/api/worlds/${encodeURIComponent(worldId)}`)
      .then(view => {
        if (cancelled) return;
        if (!view.can_edit) {
          setUniverseIssue('Only the owner of this Universe, or an admin, can start episodes in it.');
          return;
        }
        setUniverse(view);
        setSeriesId(view.world.series_id);
        setSeriesCast(view.characters);
        const genre = preselectedPreset ? null : getPresetById(view.world.profile.preset_id ?? '');
        if (genre) {
          setSelectedArchetype(genre.id);
          setTargetDuration(genre.defaultLength);
          setStep(current => (current === 'archetype' ? 'idea' : current));
        }
      })
      .catch(e => {
        if (cancelled) return;
        setUniverseIssue(e instanceof ApiError && e.status === 404
          ? 'This Universe could not be found. It may have been removed, or it is not shared with you.'
          : `The Universe could not be loaded: ${errorText(e)}`);
      });
    return () => {
      cancelled = true;
    };
  }, [worldId, preselectedPreset]);

  // Series the creator may add this video to (their own, or any for an admin)
  React.useEffect(() => {
    if (step !== 'review' || seriesList !== null || worldId) return;
    apiFetch<{ series: SeriesInfo[] }>('/api/series')
      .then(res => setSeriesList(res.series.filter(s => s.can_edit)))
      .catch(() => setSeriesList([]));
  }, [step, seriesList, worldId]);

  const chooseSeries = async (id: string) => {
    setSeriesId(id);
    setSeriesCast([]);
    setCastRows(rows => rows.map(row => ({ ...row, seriesSlug: '' })));
    if (!id) return;
    try {
      const res = await apiFetch<{ characters: CastCharacter[] }>(`/api/series/${encodeURIComponent(id)}`);
      setSeriesCast(res.characters);
    } catch (e) {
      setError(errorText(e));
    }
  };

  const patchCastRow = (code: string, patch: Partial<DraftCastRow>) => {
    setCastRows(rows => rows.map(row => (row.code === code ? { ...row, ...patch } : row)));
  };

  const handleArchetypeSelect = (archetypeId: string) => {
    const preset = getPresetById(archetypeId);
    if (!preset) return;
    setSelectedArchetype(preset.id);
    setTargetDuration(preset.defaultLength);
    setBriefAnswers({});
    setError(null);
    setStep('idea');
  };

  const handleSkipCards = () => {
    setSelectedArchetype(null);
    setTargetDuration(FREEFORM_DEFAULT_LENGTH);
    setBriefAnswers({});
    setError(null);
    setStep('idea');
  };

  const handleChangeStyle = () => {
    setError(null);
    setStep('archetype');
  };

  const handleGenerateDraft = async () => {
    if (!idea.trim()) {
      setError('Please describe your video idea');
      return;
    }

    const fullIdea = composeIdea(idea, selectedPreset?.briefFields ?? [], briefAnswers);
    const returnStep: WizardStep = draft ? 'review' : 'idea';
    setError(null);
    setBusy(true);
    setStep('loading');

    try {
      const response = await postJson<DraftResponse>('/api/episodes/draft-from-idea', {
        idea: fullIdea,
        archetype_id: selectedArchetype,
        target_duration: targetDuration,
        style_preset: stylePreset || null,
        ...(universe ? { world_id: universe.world.series_id } : {}),
      });

      setDraft(response);
      setSubmittedIdea(fullIdea);
      setCastRows(universe ? withUniverseCast(draftCastRows(response.cast), universe.characters) : draftCastRows(response.cast));
      setCastPictures({});
      setVoices(previous => voicesForDraft(response.voice_slots ?? [], previous));
      setTextOnScreen(onScreenTextDefaults(
        response.preset?.on_screen_text ?? selectedPreset?.onScreenText ?? [],
        response.audio_plan_json,
        response.title,
      ));
      setStep('review');
    } catch (e) {
      setError(errorText(e));
      setStep(returnStep);
    } finally {
      setBusy(false);
    }
  };

  const handleRegenerateDraft = async () => {
    await handleGenerateDraft();
  };

  const patchVoice = (key: string, patch: Partial<VoiceState>) => {
    setVoices(previous => ({ ...previous, [key]: { ...(previous[key] ?? emptyVoice()), ...patch } }));
  };

  /** "Find a voice" starts over; "Try another voice" leaves out every voice shown so far. */
  const findVoice = async (slot: VoiceSlot, another: boolean) => {
    const current = voices[slot.key] ?? emptyVoice();
    const description = current.description.trim();
    if (description.length < 3) {
      patchVoice(slot.key, { error: 'Describe the voice in a few words first.' });
      return;
    }
    const exclude = another ? current.shown : [];
    patchVoice(slot.key, { busy: true, error: null });
    try {
      const pick = await postJson<VoicePick>('/api/voices/pick', {
        description,
        exclude_voice_ids: exclude,
        role: slot.role === 'narrator' ? 'narrator' : slot.speaker,
      });
      patchVoice(slot.key, { busy: false, choice: pick, shown: [...exclude, pick.voice_id] });
    } catch (e) {
      patchVoice(slot.key, { busy: false, error: errorText(e) });
    }
  };

  const patchText = (patch: Partial<OnScreenTextChoices>) => {
    setTextOnScreen(previous => (previous ? { ...previous, ...patch } : previous));
  };

  const handleStartEpisode = async (dryRun: boolean) => {
    if (!draft) return;
    if (startBlock) {
      setError(startBlock);
      return;
    }

    setBusy(true);
    setError(null);

    try {
      // Upload all files
      const episodeId = draft.episode_id;
      
      // Upload beatmap
      const beatmapForm = new FormData();
      beatmapForm.append('episode_id', episodeId);
      beatmapForm.append('beatmap_file', new File([draft.beatmap], 'BEATMAP.md', { type: 'text/markdown' }));
      await apiFetch('/api/episodes/upload', { method: 'POST', body: beatmapForm });

      // The drafted characters join the episode's cast (or its series' cast); each shot then names who
      // is in it, so every still gets their description and pictures
      let shotsJson = draft.shots_json;
      let promptKit = draft.prompt_kit_json;
      if (seriesId) await postJson(`/api/episodes/${episodeId}/series`, { series_id: seriesId });
      if (castRows.length > 0) {
        const imported = await postJson<{ characters: ImportedCharacter[] }>(
          `/api/episodes/${episodeId}/cast/import`, castImportBody(castRows, seriesCast));
        shotsJson = shotsWithCast(draft.shots_json, castRows, imported.characters);
        promptKit = kitWithCast(draft.prompt_kit_json, castRows, seriesCast);
        for (const character of imported.characters) {
          const picture = character.code ? castPictures[character.code] : null;
          if (!picture) continue;
          const pictureForm = new FormData();
          pictureForm.append('image_file', picture);
          for (const [key, value] of rightsFields(castRights)) pictureForm.append(key, value);
          pictureForm.append('primary', 'true');
          await apiFetch(
            `/api/episodes/${episodeId}/cast/${character.character_id}/versions/${character.default_version}/images`,
            { method: 'POST', body: pictureForm });
        }
      }

      // Upload shots and refs
      const shotsForm = new FormData();
      shotsForm.append('shots_file', new File([JSON.stringify(shotsJson, null, 2)], 'shots.json', { type: 'application/json' }));
      shotsForm.append('refs_file', new File([JSON.stringify(draft.refs_json, null, 2)], 'refs.json', { type: 'application/json' }));
      await apiFetch(`/api/episodes/${episodeId}/upload-shots`, { method: 'POST', body: shotsForm });

      // Upload prompt kit
      const kitForm = new FormData();
      kitForm.append('prompt_kit_file', new File([JSON.stringify(promptKit, null, 2)], 'prompt_kit.json', { type: 'application/json' }));
      await apiFetch(`/api/episodes/${episodeId}/upload-prompt-kit`, { method: 'POST', body: kitForm });

      if (draft.audio_plan_json) {
        const withVoices = applyVoiceChoices(draft.audio_plan_json, voiceSlots, voiceChoices);
        const audioPlan = textOnScreen ? applyOnScreenText(withVoices, textOnScreen, draftSeconds) : withVoices;
        const audioForm = new FormData();
        audioForm.append('audio_plan_file', new File([JSON.stringify(audioPlan, null, 2)], 'audio_plan.json', { type: 'application/json' }));
        await apiFetch(`/api/episodes/${episodeId}/upload-audio-plan`, { method: 'POST', body: audioForm });
      }

      // Start episode with story shaping enabled by default
      const start: StartRequest = {
        episode_id: episodeId,
        dry_run: dryRun,
        enable_story_shaping: true,
        archetype_id: draft.archetype_id ?? selectedArchetype,
        idea: submittedIdea,
        target_duration: draft.target_duration ?? targetDuration,
      };
      // in a Universe every character keeps one locked look: until each one is locked the video waits
      // on its page, which starts it with this same request once they are; it waits there too while
      // its owner picks the shots' first frames. The studio keeps the request, so the page's Start
      // card sends it from any device.
      const keep = async () => {
        try {
          await postJson(`/api/episodes/${episodeId}/kept-start`, start);
          return true;
        } catch {
          return false;
        }
      };
      if (seriesId) {
        const info = await apiFetch<EpisodeUniverse>(`/api/episodes/${episodeId}/universe`);
        if ((info.lock?.unlocked ?? []).length > 0) {
          await keep(); // without it the page offers a free test run, and says so
          router.push(`/studio/episodes/${episodeId}#cast`);
          return;
        }
      }
      // without a kept start a video outside a Universe could not start from its page: it starts now
      if (pickFrames && await keep()) {
        router.push(`/studio/episodes/${episodeId}#first-frames`);
        return;
      }
      await postJson('/api/episodes', start);

      // Redirect to episode page
      router.push(`/studio/episodes/${episodeId}`);
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  };

  if (step === 'loading') {
    return (
      <div className="flex min-h-screen items-center justify-center px-4">
        <div className="max-w-md text-center">
          <Dots className="mb-4 motion-safe:animate-pulse" />
          <h2 className="mb-2 text-[24px]">Creating your video...</h2>
          <p className="text-ink-soft">
            Our AI is writing the script, planning shots, and setting up characters. This takes about 10-20 seconds.
          </p>
        </div>
      </div>
    );
  }

  if (worldId && !universe) {
    return (
      <div className="min-h-screen">
        <div className="max-w-2xl mx-auto px-4 sm:px-6 py-16">
          {universeIssue ? (
            <div className="pf-card p-6" data-testid="wizard-universe-problem">
              <h1 className="text-xl font-semibold mb-2">This episode can&apos;t start in that Universe</h1>
              <p className="text-sm text-gray-700">{universeIssue}</p>
              <div className="mt-4 flex flex-wrap gap-3 text-sm">
                <Link href="/studio" className="font-extrabold text-blue-deep underline">Back to the studio</Link>
                <Link href="/studio/create" className="font-extrabold text-blue-deep underline">Make a video without a Universe</Link>
              </div>
            </div>
          ) : (
            <p role="status" className="text-center text-ink-soft">Loading the Universe…</p>
          )}
        </div>
      </div>
    );
  }

  const errorBox = error && (
    <div role="alert" className="mb-6 rounded-inner bg-coral-soft p-4 text-[13.5px] font-bold text-coral-ink">
      {error}
    </div>
  );
  const visibleGroups = CARD_GROUPS.filter(group => !templateFilter || group.template === templateFilter);
  const filterClass = (active: boolean) =>
    `px-3 py-1 rounded-full border text-sm ${
      active
        ? 'bg-blue-deep text-white border-blue-deep'
        : 'bg-white border-transparent shadow-card hover:border-blue-deep'
    }`;

  const order = ['archetype', 'idea', 'review'];
  const at = order.indexOf(step);
  const trail: JourneyStop[] = [
    { key: 'archetype', name: 'Style', note: selectedPreset?.name ?? (at > 0 ? 'Freeform' : 'Pick one'), state: at > 0 ? 'done' : 'now' },
    { key: 'idea', name: 'Idea', note: at > 1 ? 'Written' : 'A few sentences', state: at > 1 ? 'done' : at === 1 ? 'now' : 'todo' },
    { key: 'review', name: 'Review & start', note: 'Price shown first', state: at === 2 ? 'now' : 'todo', paid: true },
    { key: 'episode', name: 'Episode page', note: 'Approve each step', state: 'todo' },
  ];

  const universeColours = castColours(seriesCast);
  const shotEnds: number[] = [];
  for (const shot of draft?.shots_json?.shots ?? []) shotEnds.push((shotEnds[shotEnds.length - 1] ?? 0) + (Number(shot.duration) || 0));
  const scriptLines: { id: string; text: string; speaker: string | null; shot: number | null }[] =
    (draft?.audio_plan_json?.vo?.lines ?? [])
      .filter((line: { text?: string }) => line?.text)
      .map((line: { id?: string; text: string; speaker?: string; at?: number }, i: number) => {
        const at = Number(line.at) || 0;
        const index = shotEnds.findIndex((end) => at < end);
        return { id: line.id ?? `L${i + 1}`, text: line.text, speaker: line.speaker?.trim() || null, shot: index >= 0 ? index + 1 : null };
      });
  const speakers = speakerColours(['', ...scriptLines.map((line) => line.speaker ?? '')], seriesCast);

  return (
    <div className="min-h-screen pb-12">
      <div className="mx-auto max-w-[1100px] px-4 pt-3 sm:px-8 sm:pt-6">
        <HeroBand className="px-5 pb-6 pt-5 sm:px-10 sm:pb-8 sm:pt-7">
          <Link href="/studio" className="text-[13px] font-extrabold text-blue-deep">
            Back to the studio
          </Link>
          <h1 className="mt-1 text-[32px] leading-tight sm:text-[44px]">Make Your Own Video</h1>
          <p className="text-[15px] text-ink-soft">
            Create an original short video in minutes. No coding required.
          </p>
          {universe && (
            <p className="pf-chip mt-2 bg-white text-ink shadow-card" data-testid="wizard-universe">
              In:{' '}
              <Link href={`/studio/universes/${encodeURIComponent(universe.world.series_id)}`}
                className="font-extrabold text-blue-deep underline">
                {universe.world.name}
              </Link>
            </p>
          )}
          <div className="mx-auto mt-4 max-w-[760px] rounded-card bg-white/75 px-3 pb-1 pt-3 backdrop-blur-md sm:px-5">
            <JourneyPath stops={trail} current={step} label="Steps to make a video" compact />
          </div>
        </HeroBand>
      </div>

      <div className="mx-auto max-w-[1100px] px-4 py-8 sm:px-8">
        {step !== 'review' && errorBox}

        {step === 'archetype' && (
          <section>
            <h2 className="mb-2 text-[26px]">Step 1: Choose a Style</h2>
            <p className="mb-6 text-ink-soft">
              Pick a video type. It sets the length range, picture format and sound; you tell the story next.
            </p>

            <div className="flex flex-wrap gap-2 mb-6" role="group" aria-label="Filter by template">
              <button
                type="button"
                aria-pressed={!templateFilter}
                onClick={() => setTemplateFilter(null)}
                className={filterClass(!templateFilter)}
                data-testid="template-filter-all"
              >
                All ({presets.length})
              </button>
              {CARD_GROUPS.map(group => (
                <button
                  key={group.template}
                  type="button"
                  aria-pressed={templateFilter === group.template}
                  onClick={() => setTemplateFilter(group.template)}
                  className={filterClass(templateFilter === group.template)}
                  data-testid={`template-filter-${group.template}`}
                >
                  {group.label} ({group.presets.length})
                </button>
              ))}
            </div>

            {visibleGroups.map(group => (
              <div key={group.template} className="mb-8" data-testid={`preset-group-${group.template}`}>
                <h3 className="mb-3 text-[19px]">{group.label}</h3>
                <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
                  {group.presets.map((preset) => (
                    <button
                      key={preset.id}
                      type="button"
                      onClick={() => handleArchetypeSelect(preset.id)}
                      className="pf-card border-2 border-transparent p-4 text-left transition-[transform,border-color] hover:-translate-y-0.5 hover:border-blue-deep motion-reduce:transform-none"
                      data-testid={`preset-card-${preset.id}`}
                    >
                      <h4 className="mb-1 text-[16px]">{preset.name}</h4>
                      <p className="mb-2 text-[13.5px] text-ink-soft">{preset.description}</p>
                      <div className="flex flex-wrap gap-x-2 text-[12px] font-bold text-ink-soft">
                        <span data-testid={`card-length-${preset.id}`}>{preset.lengthRange[0]}–{preset.lengthRange[1]}s</span>
                        <span>·</span>
                        <span data-testid={`card-aspect-${preset.id}`}>{formatAspect(preset.aspect)}</span>
                        <span>·</span>
                        <span data-testid={`card-sound-${preset.id}`}>{soundLabel(preset)}</span>
                      </div>
                    </button>
                  ))}
                </div>
              </div>
            ))}

            <button
              type="button"
              onClick={handleSkipCards}
              className="text-[13.5px] font-extrabold text-blue-deep underline"
            >
              Skip - I'll describe my style in the next step
            </button>
          </section>
        )}

        {step === 'idea' && (
          <section className="space-y-6">
            <div>
              <button
                type="button"
                onClick={handleChangeStyle}
                className="text-[13.5px] font-extrabold text-blue-deep underline"
                data-testid="change-style"
              >
                Change style
              </button>
            </div>

            {selectedPreset ? (
              <div className="rounded-inner bg-blue-soft p-4" data-testid="selected-style">
                <p className="text-sm font-semibold text-blue-900">Style: {selectedPreset.name}</p>
                <p className="text-xs text-blue-700">{selectedPreset.description}</p>
                <p className="text-xs text-blue-700 mt-1">
                  {TEMPLATE_LABELS[selectedPreset.template] ?? selectedPreset.template} · {selectedPreset.lengthRange[0]}–{selectedPreset.lengthRange[1]}s · {formatAspect(selectedPreset.aspect)} · {soundLabel(selectedPreset)}
                </p>
              </div>
            ) : (
              <div className="rounded-inner bg-mist p-4" data-testid="selected-style">
                <p className="text-sm font-semibold">Style: Freeform</p>
                <p className="text-xs text-gray-600">
                  No video type picked: describe the style in your own words. {FREEFORM_LENGTH_RANGE[0]}–{FREEFORM_LENGTH_RANGE[1]}s · 9:16.
                </p>
              </div>
            )}

            <div>
              <label className="mb-2 block font-display text-[26px]">Step 2: Describe Your Video</label>
              <p className="mb-4 text-[13.5px] text-ink-soft">
                Tell us about your story, characters, setting, and mood. A few sentences is enough.
              </p>
              <textarea
                value={idea}
                onChange={(e) => setIdea(e.target.value)}
                placeholder="Example: A curious robot explores an abandoned library filled with glowing books. It discovers a special book that comes alive with magical creatures. The mood is mysterious and wonder-filled."
                rows={6}
                className="pf-input w-full text-base"
                data-testid="idea-input"
              />
            </div>

            {selectedPreset && selectedPreset.briefFields.length > 0 && (
              <fieldset className="pf-card p-4" data-testid="brief-fields">
                <legend className="px-1 text-sm font-semibold">Optional details for {selectedPreset.name}</legend>
                <p className="text-xs text-gray-500 mb-3">Fill in any that help; leave the rest blank.</p>
                <div className="grid sm:grid-cols-2 gap-3">
                  {selectedPreset.briefFields.map(field => (
                    <label key={field} className="block text-sm">
                      <span className="block font-medium mb-1">{briefFieldLabel(field)}</span>
                      <input
                        type="text"
                        value={briefAnswers[field] ?? ''}
                        onChange={(e) => setBriefAnswers(previous => ({ ...previous, [field]: e.target.value }))}
                        placeholder="Optional"
                        maxLength={300}
                        className="pf-input w-full"
                        data-testid={`brief-${field}`}
                      />
                    </label>
                  ))}
                </div>
              </fieldset>
            )}

            <div className="grid sm:grid-cols-2 gap-4">
              <div>
                <label htmlFor="target-length" className="block text-sm font-semibold mb-2">Target Length</label>
                <select
                  id="target-length"
                  value={targetDuration}
                  onChange={(e) => setTargetDuration(Number(e.target.value))}
                  className="pf-input w-full"
                  data-testid="duration-select"
                >
                  {lengthOptions(lengthRange, recommendedLength).map(seconds => (
                    <option key={seconds} value={seconds}>
                      {seconds} seconds{seconds === recommendedLength ? ' (recommended)' : ''}
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label htmlFor="style-preset" className="block text-sm font-semibold mb-2">Style (optional)</label>
                <input
                  id="style-preset"
                  type="text"
                  value={stylePreset}
                  onChange={(e) => setStylePreset(e.target.value)}
                  placeholder="e.g., anime, watercolor, cinematic"
                  className="pf-input w-full"
                  data-testid="style-input"
                />
              </div>
            </div>

            <div className="rounded-inner bg-mustard-soft p-4 text-sm">
              <p className="font-semibold text-yellow-900 mb-2">
                Original work only
              </p>
              <p className="text-yellow-800">
                Create only original characters and stories. No copyrighted characters, real people, or brand names. 
                If your idea includes them, we'll create original alternatives.
              </p>
            </div>

            <button
              onClick={handleGenerateDraft}
              disabled={busy || !idea.trim()}
              className="pf-btn pf-btn-primary w-full"
              data-testid="generate-button"
            >
              Generate Video Plan
            </button>
          </section>
        )}

        {step === 'review' && draft && (
          <section className="space-y-6">
            <div>
              <h2 className="mb-1 text-[26px]">Step 3: Review & Start</h2>
              <p className="text-[13.5px] text-ink-soft">
                We've created a complete plan for your video. Review it and start production.
              </p>
            </div>

            {universe && castRows.length > 0 && (
              <section className="pf-card p-6" data-testid="wizard-from-universe" aria-labelledby="from-universe-title">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <h3 id="from-universe-title" className="text-[19px]">From {universe.world.name}</h3>
                  <p className="text-[12.5px] text-ink-soft">Every episode reuses these exact looks.</p>
                </div>
                <ul className="mt-3 flex flex-wrap gap-2.5">
                  {castRows.map(row => {
                    const same = row.seriesSlug ? seriesCast.find(c => c.slug === row.seriesSlug) : undefined;
                    const match = castMatchText(same);
                    const locked = match.badge?.tone === 'ok';
                    return (
                      <li key={row.code} data-testid={`wizard-from-${row.code}`}
                        className={`flex min-w-0 items-center gap-2 rounded-full bg-white py-1.5 pl-3 pr-1.5 ${locked ? 'shadow-card' : 'shadow-[0_0_0_3px_var(--coral),var(--shadow-1)]'}`}>
                        {same && <CastDot colour={universeColours[same.slug]} />}
                        <span className="truncate text-[13.5px] font-extrabold">{same?.name ?? (row.name || row.code)}</span>
                        <span className={`${chip} ${tones[match.badge?.tone ?? 'info']}`}>{match.badge?.label ?? 'New to this Universe'}</span>
                      </li>
                    );
                  })}
                </ul>
                {castRows.some(row => {
                  const same = row.seriesSlug ? seriesCast.find(c => c.slug === row.seriesSlug) : undefined;
                  return castMatchText(same).badge?.tone !== 'ok';
                }) && (
                  <p className="mt-3 rounded-inner bg-coral-soft p-3 text-[13px] font-bold text-coral-ink">
                    A character with a coral ring has no locked look yet: the video waits on its page until you lock it there.
                  </p>
                )}
              </section>
            )}

            {scriptLines.length > 0 && (
              <section className="pf-card p-6" data-testid="wizard-script" aria-labelledby="script-title">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h3 id="script-title" className="text-[19px]">Script</h3>
                  <span className="flex gap-1.5">
                    <span className={`${chip} ${tones.run}`}>Dialogue preview</span>
                    <span className={`${chip} ${tones.wait}`}>Table read · coming</span>
                  </span>
                </div>
                <ol className="mt-3 divide-y divide-line">
                  {scriptLines.map((line, i) => (
                    <li key={`${line.id}-${i}`} className="grid grid-cols-[minmax(0,8.5rem)_1fr_auto] items-start gap-3 py-2.5 text-[14px]">
                      <SpeakerChip name={line.speaker} colour={speakers[line.speaker ?? ''] ?? speakers['']} className="w-full" />
                      <span className="min-w-0 break-words">{line.text}</span>
                      <span className="whitespace-nowrap text-[12px] font-bold text-ink-soft">{line.shot ? `Shot ${line.shot}` : ''}</span>
                    </li>
                  ))}
                </ol>
                <p className="mt-2 text-[12px] text-ink-soft">Each speaker keeps their colour and saved voice from the Universe.</p>
              </section>
            )}

            {draft.warnings && draft.warnings.length > 0 && (
              <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
                {draft.warnings.map((warning, i) => (
                  <p key={i} className="text-sm text-blue-800">{warning}</p>
                ))}
              </div>
            )}

            <div className="pf-card p-6">
              <h3 className="mb-3 text-[19px]">Story Overview</h3>
              <div className="space-y-2 text-sm">
                <p>
                  <span className="font-medium">Episode:</span> {draft.episode_id}
                </p>
                {draft.preset && (
                  <p data-testid="review-video-type">
                    <span className="font-medium">Video type:</span> {draft.preset.name}
                  </p>
                )}
                {draft.target_duration != null && (
                  <p data-testid="review-length">
                    <span className="font-medium">Length:</span> {draft.target_duration} seconds
                  </p>
                )}
                <p>
                  <span className="font-medium">Shots:</span> {draft.shots_json.shots.length}
                </p>
                {draft.cost_estimate && (
                  <p className="text-lg font-semibold mt-2" data-testid="estimated-cost">
                    Estimated cost: {formatUsd(draft.cost_estimate.total_usd_micros)}
                  </p>
                )}
              </div>
            </div>

            <div className="pf-card p-6" data-testid="review-cast">
              <h3 className="mb-3 text-[19px]">Characters</h3>
              {castRows.length === 0 ? (
                <div className="space-y-2">
                  {Object.entries(draft.prompt_kit_json.characters || {}).map(([code, char]: [string, any]) => (
                    <div key={code} className="text-sm">
                      <span className="font-medium">{code}:</span> {char.description}
                    </div>
                  ))}
                </div>
              ) : (
                <>
                  {universe ? (
                    <p className="text-sm text-gray-600 mb-4" data-testid="universe-cast-note">
                      Characters already in {universe.world.name} keep their name, look and locked sheet. Anyone new
                      joins the Universe when this video is set up. Every character needs a locked look before the
                      video starts: you lock a new one with a character sheet on the video&apos;s page.
                    </p>
                  ) : (
                    <p className="text-sm text-gray-600 mb-4">
                      Everyone here joins your video&apos;s cast. Their name and look go into every shot they are in, so
                      describe the look the same way you want it on screen. A picture is optional; on the video&apos;s
                      page you can lock each look with a character sheet.
                    </p>
                  )}
                  {!universe && seriesList && seriesList.length > 0 && (
                    <label className="block text-sm mb-4">
                      <span className="block font-medium mb-1">Universe (optional)</span>
                      <select
                        value={seriesId}
                        onChange={(e) => chooseSeries(e.target.value)}
                        className="pf-input w-full"
                        data-testid="cast-series-select"
                      >
                        <option value="">Only this video</option>
                        {seriesList.map(s => <option key={s.series_id} value={s.series_id}>{s.name}</option>)}
                      </select>
                      <span className="block text-xs text-gray-500 mt-1">
                        Pick a Universe to reuse its cast and their locked looks.
                      </span>
                    </label>
                  )}
                  <div className="space-y-4">
                    {castRows.map(row => {
                      const same = row.seriesSlug ? seriesCast.find(c => c.slug === row.seriesSlug) : undefined;
                      const picture = castPictures[row.code] ?? null;
                      const pictureIssue = picture ? pictureProblem(picture, null) : null;
                      const match = universe ? castMatchText(same) : null;
                      return (
                        <div
                          key={row.code}
                          className="border-t border-gray-200 pt-4 first:border-t-0 first:pt-0"
                          data-testid={`cast-row-${row.code}`}
                        >
                          {match && (
                            <p className="mb-2">
                              <span className={`${chip} ${tones[match.badge?.tone ?? 'todo']}`} data-testid={`cast-match-${row.code}`}
                                data-matched={match.matched}>
                                {match.text}
                              </span>
                            </p>
                          )}
                          {seriesCast.length > 0 && (
                            <label className="block text-sm mb-2">
                              <span className="block font-medium mb-1">Same as in the Universe</span>
                              <select
                                value={row.seriesSlug}
                                onChange={(e) => {
                                  patchCastRow(row.code, { seriesSlug: e.target.value });
                                  setCastPictures(previous => ({ ...previous, [row.code]: null }));
                                }}
                                className="pf-input w-full"
                                data-testid={`cast-same-${row.code}`}
                              >
                                <option value="">{universe ? 'No, new to this Universe' : 'No, new in this video'}</option>
                                {seriesCast.map(c => <option key={c.slug} value={c.slug}>{c.name}</option>)}
                              </select>
                            </label>
                          )}
                          {same ? (
                            <p className="text-sm">
                              <span className="font-medium">{same.name}</span>
                              {same.description ? `: ${same.description}` : ''}
                            </p>
                          ) : (
                            <div className="grid sm:grid-cols-2 gap-2">
                              <input
                                type="text"
                                value={row.name}
                                onChange={(e) => patchCastRow(row.code, { name: e.target.value })}
                                maxLength={60}
                                aria-label="Name"
                                placeholder="Name"
                                className="pf-input"
                                data-testid={`cast-name-${row.code}`}
                              />
                              <input
                                type="text"
                                value={row.role}
                                onChange={(e) => patchCastRow(row.code, { role: e.target.value })}
                                maxLength={80}
                                aria-label="Part they play"
                                placeholder="Part they play, e.g. the lead"
                                className="pf-input"
                              />
                              <textarea
                                value={row.description}
                                onChange={(e) => patchCastRow(row.code, { description: e.target.value })}
                                maxLength={600}
                                rows={2}
                                aria-label="What they look like"
                                placeholder="What they look like: age, build, face, hair, colours, clothing"
                                className="pf-input sm:col-span-2"
                                data-testid={`cast-description-${row.code}`}
                              />
                              <label className="sm:col-span-2 text-sm">
                                <span className="block font-medium mb-1">Picture (optional)</span>
                                <input
                                  type="file"
                                  accept={PICTURE_ACCEPT}
                                  onChange={(e) => {
                                    const file = e.target.files?.[0] ?? null;
                                    setCastPictures(previous => ({ ...previous, [row.code]: file }));
                                  }}
                                  className="text-sm"
                                  data-testid={`cast-picture-${row.code}`}
                                />
                              </label>
                              {pictureIssue && <p className="sm:col-span-2 text-xs text-red-700">{pictureIssue}</p>}
                              {picture && !pictureIssue && (
                                <ChosenPictureSize file={picture} limits={null} testId={`cast-picture-size-${row.code}`}
                                  className="sm:col-span-2" />
                              )}
                            </div>
                          )}
                          {row.shots.length > 0 && (
                            <p className="text-xs text-gray-500 mt-1">In {row.shots.join(', ')}</p>
                          )}
                        </div>
                      );
                    })}
                  </div>
                  {chosenPictures.length > 0 && (
                    <fieldset className="mt-4 text-sm space-y-1" data-testid="cast-rights">
                      <legend className="font-medium mb-1">Whose pictures are these?</legend>
                      <label className="flex items-center gap-2">
                        <input
                          type="radio"
                          name="cast-rights"
                          checked={castRights.rights === 'original'}
                          onChange={() => setCastRights({ ...castRights, rights: 'original' })}
                          data-testid="cast-rights-original"
                        />
                        I made them myself (my own original work)
                      </label>
                      <label className="flex items-center gap-2">
                        <input
                          type="radio"
                          name="cast-rights"
                          checked={castRights.rights === 'owned'}
                          onChange={() => setCastRights({ ...castRights, rights: 'owned' })}
                          data-testid="cast-rights-owned"
                        />
                        I own the rights to use them
                      </label>
                      <label className="flex items-center gap-2">
                        <input
                          type="checkbox"
                          checked={castRights.realPerson}
                          onChange={(e) => setCastRights({ ...castRights, realPerson: e.target.checked, likenessConsent: false })}
                          data-testid="cast-real-person"
                        />
                        One of them shows a real person
                      </label>
                      {castRights.realPerson && (
                        <label className="flex items-center gap-2 ml-6">
                          <input
                            type="checkbox"
                            checked={castRights.likenessConsent}
                            onChange={(e) => setCastRights({ ...castRights, likenessConsent: e.target.checked })}
                            data-testid="cast-likeness-consent"
                          />
                          They agreed to their likeness being used in this video
                        </label>
                      )}
                    </fieldset>
                  )}
                </>
              )}
            </div>

            {voiceSlots.length > 0 && (
              <div className="pf-card p-6" data-testid="voices-section">
                <h3 className="mb-1 text-[19px]">Voices</h3>
                <p className="text-sm text-gray-600 mb-4">
                  Describe each voice in plain words and we pick a matching one. Play it, and try another until it fits.
                  {universe && Object.keys(savedVoices).length > 0 && ` The voices saved in ${universe.world.name} are filled in.`}
                </p>
                <div className="space-y-4">
                  {voiceSlots.map(slot => {
                    const saved = savedVoices[slot.key];
                    if (saved && !ownVoice[slot.key]) {
                      return (
                        <SavedVoiceSlot key={slot.key} slot={slot} pick={saved}
                          onChange={() => setOwnVoice(previous => ({ ...previous, [slot.key]: true }))} />
                      );
                    }
                    const picker = (
                      <VoiceSlotPicker
                        key={slot.key}
                        slot={slot}
                        state={voices[slot.key] ?? emptyVoice()}
                        onDescription={(text) => patchVoice(slot.key, { description: text })}
                        onFind={() => findVoice(slot, false)}
                        onRetry={() => findVoice(slot, true)}
                      />
                    );
                    if (!saved) return picker;
                    return (
                      <div key={slot.key} className="border-t border-gray-200 pt-4 first:border-t-0 first:pt-0">
                        {picker}
                        <button
                          type="button"
                          onClick={() => setOwnVoice(previous => ({ ...previous, [slot.key]: false }))}
                          className="mt-2 text-sm font-extrabold text-blue-deep underline"
                          data-testid={`voice-saved-back-${slotToken(slot.key)}`}
                        >
                          Use the saved voice ({saved.name || 'unnamed voice'}) instead
                        </button>
                      </div>
                    );
                  })}
                </div>
                {draft.default_voice_available && (
                  <p className="text-xs text-gray-500 mt-4">Lines without a chosen voice use the studio's default voice.</p>
                )}
              </div>
            )}

            {textOnScreen && (
              <div className="pf-card p-6" data-testid="on-screen-text">
                <h3 className="mb-1 text-[19px]">Text on screen</h3>
                <p className="text-sm text-gray-600 mb-4">
                  {draft.preset ? `Set up the way ${draft.preset.name} videos usually look. ` : ''}
                  A subtitle file (.srt) comes with the video either way.
                </p>
                <div className="space-y-3 text-sm">
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={textOnScreen.subtitles}
                      disabled={!hasVoiceLines(draft.audio_plan_json)}
                      onChange={(e) => patchText({ subtitles: e.target.checked })}
                      data-testid="ost-subtitles"
                    />
                    <span>
                      Subtitles burned into the picture
                      {!hasVoiceLines(draft.audio_plan_json) && ' (this draft has no voice-over)'}
                    </span>
                  </label>
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={textOnScreen.titleCard}
                      onChange={(e) => patchText({ titleCard: e.target.checked })}
                      data-testid="ost-title-card"
                    />
                    <span>Opening title card</span>
                  </label>
                  {textOnScreen.titleCard && (
                    <input
                      type="text"
                      value={textOnScreen.titleText}
                      onChange={(e) => patchText({ titleText: e.target.value })}
                      maxLength={TEXT_CARD_MAX_CHARS}
                      aria-label="Title card text"
                      placeholder="Episode title"
                      className="pf-input w-full"
                      data-testid="ost-title-text"
                    />
                  )}
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={textOnScreen.endCard}
                      onChange={(e) => patchText({ endCard: e.target.checked })}
                      data-testid="ost-end-card"
                    />
                    <span>End card</span>
                  </label>
                  {textOnScreen.endCard && (
                    <input
                      type="text"
                      value={textOnScreen.endText}
                      onChange={(e) => patchText({ endText: e.target.value })}
                      maxLength={TEXT_CARD_MAX_CHARS}
                      aria-label="End card text"
                      className="pf-input w-full"
                      data-testid="ost-end-text"
                    />
                  )}
                </div>
              </div>
            )}

            <div className="pf-card p-6">
              <h3 className="mb-3 text-[19px]">Shot List</h3>
              <div className="space-y-3">
                {draft.shots_json.shots.map((shot: any, i: number) => (
                  <div key={shot.id} className="border-l-4 border-blue-500 pl-4 py-2">
                    <p className="font-semibold text-sm">Shot {i + 1}: {shot.id}</p>
                    <p className="text-sm text-gray-600">{shot.prompt}</p>
                    <p className="text-xs text-gray-500 mt-1">{shot.duration}s · {shot.model}</p>
                  </div>
                ))}
              </div>
            </div>

            <div className="space-y-4">
              <button
                onClick={handleRegenerateDraft}
                disabled={busy}
                className="pf-btn pf-btn-quiet w-full"
                data-testid="regenerate-button"
              >
                Regenerate Plan
              </button>

              <div className="border-t border-gray-200 pt-4">
                {errorBox}
                {voiceBlock && (
                  <p
                    className="mb-4 rounded-inner bg-mustard-soft p-3 text-[13.5px] font-bold text-mustard-ink"
                    data-testid="voice-block"
                  >
                    {voiceBlock}
                  </p>
                )}
                {textBlock && (
                  <p
                    className="mb-4 rounded-inner bg-mustard-soft p-3 text-[13.5px] font-bold text-mustard-ink"
                    data-testid="text-block"
                  >
                    {textBlock}
                  </p>
                )}
                {!voiceBlock && !textBlock && castBlock && (
                  <p
                    className="mb-4 rounded-inner bg-mustard-soft p-3 text-[13.5px] font-bold text-mustard-ink"
                    data-testid="cast-block"
                  >
                    {castBlock}
                  </p>
                )}
                {universe && (
                  <p className="text-sm text-gray-700 mb-4" data-testid="universe-lock-note">
                    This video is in {universe.world.name}, so it starts once every character in it has a locked look.
                    If one doesn&apos;t yet, the video waits on its page: make and lock the character sheet there, then start it.
                  </p>
                )}
                <label className="mb-4 flex cursor-pointer items-start gap-3 rounded-inner bg-blue-soft p-3"
                  data-testid="pick-frames-option">
                  <input type="checkbox" checked={pickFrames} onChange={(e) => setPickFrames(e.target.checked)}
                    className="mt-0.5 size-5 flex-none accent-blue-deep" data-testid="pick-frames-checkbox" />
                  <span className="min-w-0 text-[13.5px]">
                    <span className="block font-extrabold">Pick each shot&apos;s first frame before filming</span>
                    <span className="mt-0.5 block text-ink-soft">
                      The video waits on its page while you see a few options for each shot and pick the one filming
                      starts from, then you start it there. Optional: without it the studio makes them while filming.
                    </span>
                  </span>
                </label>
                <p className="text-sm font-semibold mb-3">Choose how to proceed:</p>
                
                <div className="space-y-3">
                  {liveAvailable ? (
                    <>
                      <button
                        onClick={() => setShowLiveConfirm(true)}
                        disabled={busy || !!startBlock || !draft.cost_estimate}
                        className="pf-btn pf-btn-go w-full !justify-between"
                        data-testid="make-video-button"
                      >
                        <span>Make My Video (uses credits)</span>
                        {draft.cost_estimate && (
                          <span className="text-sm">{formatUsd(draft.cost_estimate.total_usd_micros)}</span>
                        )}
                      </button>
                      {!draft.cost_estimate && (
                        <p className="text-[12.5px] text-ink-soft" data-testid="make-video-no-price">
                          We couldn&apos;t get a price for this plan, so a live run stays off. Regenerate the plan to try
                          again, or preview it for free.
                        </p>
                      )}
                    </>
                  ) : (
                    <div className="w-full rounded-input border-2 border-dashed border-line-strong px-5 py-3">
                      <div className="text-[13px] text-ink-soft">
                        <p className="font-semibold mb-1">Live mode not available</p>
                        <p className="text-xs">Server is in dry-run mode. Use preview to test.</p>
                      </div>
                    </div>
                  )}

                  <button
                    onClick={() => handleStartEpisode(true)}
                    disabled={busy || !!startBlock}
                    className="pf-btn pf-btn-primary w-full"
                    data-testid="preview-button"
                  >
                    Preview Only (free test run)
                  </button>
                </div>

                <p className="text-xs text-gray-500 mt-3">
                  Live mode uses real credits and charges you. Preview mode is always free.
                </p>
              </div>
            </div>

            {showLiveConfirm && (
              <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/50 p-4">
                <div className="pf-card w-full max-w-md p-6" role="dialog" aria-modal="true" aria-labelledby="live-confirm-title">
                  <h3 id="live-confirm-title" className="mb-3 text-[20px]">Confirm Live Production</h3>
                  <p className="text-sm text-gray-600 mb-4">
                    This will start a live production run that uses real credits and charges you.
                    {pickFrames && ' It starts from the video\u2019s page once you have picked its first frames.'}
                  </p>
                  {draft.cost_estimate && (
                    <div className="mb-4 rounded-inner border-2 border-mustard bg-mustard-soft p-3">
                      <p className="text-sm font-extrabold text-ink">
                        Estimated cost: {formatUsd(draft.cost_estimate.total_usd_micros)}
                      </p>
                      <p className="mt-1 text-xs text-ink-soft">
                        This is an estimate. Actual cost may vary based on retries and model usage.
                      </p>
                    </div>
                  )}
                  <div className="flex gap-3">
                    <button
                      onClick={() => setShowLiveConfirm(false)}
                      className="pf-btn pf-btn-quiet flex-1"
                    >
                      Cancel
                    </button>
                    <button
                      onClick={() => {
                        setShowLiveConfirm(false);
                        handleStartEpisode(false);
                      }}
                      className="pf-btn pf-btn-go flex-1"
                      data-testid="confirm-live-button"
                    >
                      Confirm & Start
                    </button>
                  </div>
                </div>
              </div>
            )}
          </section>
        )}

        <div className="mt-8">
          <Link href="/studio/episodes/new" className="text-[13px] font-bold text-ink-soft underline">
            Advanced: Upload files manually
          </Link>
        </div>
      </div>
    </div>
  );
}

export default function CreateVideoPage() {
  return (
    <Suspense fallback={
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="text-center">
          <div className="animate-spin rounded-full h-16 w-16 border-b-2 border-black mx-auto mb-4"></div>
          <p className="text-gray-600">Loading...</p>
        </div>
      </div>
    }>
      <CreateVideoWizard />
    </Suspense>
  );
}
