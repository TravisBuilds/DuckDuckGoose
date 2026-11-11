'use client';

import React, { Suspense, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { formatUsd } from '@/lib/money';
import { apiFetch, postJson } from '@/lib/studio-api';
import { presets, getPresetById } from '@/lib/presets';
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
  slotToken,
  soundLabel,
  voiceBlockMessage,
  type OnScreenTextChoices,
  type VoiceChoices,
  type VoicePick,
  type VoiceSlot,
} from '@/lib/wizard.mjs';

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
}

interface VoiceState {
  description: string;
  choice: VoicePick | null;
  shown: string[];
  busy: boolean;
  error: string | null;
}

type WizardStep = 'archetype' | 'idea' | 'review' | 'loading';

const CARD_GROUPS = groupByTemplate(presets);

function emptyVoice(): VoiceState {
  return { description: '', choice: null, shown: [], busy: false, error: null };
}

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** Voice state for a new draft: slots that are still there keep their description and voice. */
function voicesForDraft(slots: VoiceSlot[], previous: Record<string, VoiceState>): Record<string, VoiceState> {
  return Object.fromEntries(
    slots.map(slot => [slot.key, { ...(previous[slot.key] ?? emptyVoice()), busy: false, error: null }]),
  );
}

function VoicePreview({ pick, token }: { pick: VoicePick; token: string }) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [playError, setPlayError] = useState<string | null>(null);

  if (!pick.preview_url) {
    return <span className="text-xs text-gray-500">No preview for this voice</span>;
  }

  const toggle = async () => {
    const audio = audioRef.current;
    if (!audio) return;
    if (!audio.paused) {
      audio.pause();
      audio.currentTime = 0;
      return;
    }
    setPlayError(null);
    try {
      await audio.play();
    } catch (e) {
      setPlayError(`Could not play the preview: ${errorText(e)}`);
    }
  };

  return (
    <>
      <button
        type="button"
        onClick={toggle}
        className="px-3 py-1.5 border border-gray-300 dark:border-gray-600 rounded-lg text-sm font-semibold hover:bg-gray-50 dark:hover:bg-gray-700"
        data-testid={`voice-play-${token}`}
      >
        {playing ? '■ Stop' : '▶ Play preview'}
      </button>
      <audio
        ref={audioRef}
        src={pick.preview_url}
        preload="none"
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        data-testid={`voice-audio-${token}`}
      />
      {playError && <span className="text-xs text-red-700 dark:text-red-300">{playError}</span>}
    </>
  );
}

function VoiceSlotPicker({
  slot,
  state,
  onDescription,
  onFind,
  onRetry,
}: {
  slot: VoiceSlot;
  state: VoiceState;
  onDescription: (text: string) => void;
  onFind: () => void;
  onRetry: () => void;
}) {
  const token = slotToken(slot.key);
  const inputId = `voice-description-${token}`;
  const choice = state.choice;
  return (
    <div className="border-t border-gray-200 dark:border-gray-700 pt-4 first:border-t-0 first:pt-0" data-testid={`voice-slot-${token}`}>
      <label htmlFor={inputId} className="block text-sm font-semibold">
        {slot.label}: describe the voice
      </label>
      <p className="text-xs text-gray-500 mb-2">
        {slot.lines} voice-over line{slot.lines === 1 ? '' : 's'}
      </p>
      <div className="flex flex-col sm:flex-row gap-2">
        <input
          id={inputId}
          type="text"
          value={state.description}
          onChange={(e) => onDescription(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !state.busy) onFind();
          }}
          maxLength={300}
          placeholder={`e.g. ${slot.suggestion}`}
          className="flex-1 px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900 text-sm"
          data-testid={inputId}
        />
        <button
          type="button"
          onClick={onFind}
          disabled={state.busy || state.description.trim().length < 3}
          className="px-4 py-2 bg-black dark:bg-white text-white dark:text-black rounded-lg text-sm font-semibold disabled:opacity-50"
          data-testid={`voice-find-${token}`}
        >
          {state.busy ? 'Finding…' : 'Find a voice'}
        </button>
      </div>
      {state.error && (
        <p className="mt-2 text-sm text-red-700 dark:text-red-300" data-testid={`voice-error-${token}`}>
          {state.error}
        </p>
      )}
      {choice && (
        <div
          className="mt-3 flex flex-wrap items-center gap-3 bg-gray-50 dark:bg-gray-900/40 rounded-lg p-3"
          data-testid={`voice-choice-${token}`}
          data-voice-id={choice.voice_id}
        >
          <span className="text-sm">
            Voice: <strong data-testid={`voice-name-${token}`}>{choice.name}</strong>
          </span>
          <VoicePreview key={choice.voice_id} pick={choice} token={token} />
          <button
            type="button"
            onClick={onRetry}
            disabled={state.busy}
            className="px-3 py-1.5 border border-gray-300 dark:border-gray-600 rounded-lg text-sm font-semibold hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50"
            data-testid={`voice-retry-${token}`}
          >
            Try another voice
          </button>
          {choice.reason && <p className="w-full text-xs text-gray-500">{choice.reason}</p>}
        </div>
      )}
    </div>
  );
}

function CreateVideoWizard() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const preselectedPreset = getPresetById(searchParams?.get('archetype') ?? '');

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
  const [liveAvailable, setLiveAvailable] = useState(false);

  const selectedPreset = selectedArchetype ? getPresetById(selectedArchetype) ?? null : null;
  const lengthRange = selectedPreset?.lengthRange ?? FREEFORM_LENGTH_RANGE;
  const recommendedLength = selectedPreset?.defaultLength ?? FREEFORM_DEFAULT_LENGTH;
  const voiceSlots = draft?.voice_slots ?? [];
  const voiceChoices: VoiceChoices = Object.fromEntries(
    Object.entries(voices).map(([key, state]) => [key, state.choice]),
  );
  const voiceBlock = draft ? voiceBlockMessage(voiceSlots, voiceChoices, !!draft.default_voice_available) : null;
  const textBlock = draft ? onScreenTextProblem(textOnScreen) : null;
  const startBlock = voiceBlock ?? textBlock;
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
      });

      setDraft(response);
      setSubmittedIdea(fullIdea);
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

      // Upload shots and refs
      const shotsForm = new FormData();
      shotsForm.append('shots_file', new File([JSON.stringify(draft.shots_json, null, 2)], 'shots.json', { type: 'application/json' }));
      shotsForm.append('refs_file', new File([JSON.stringify(draft.refs_json, null, 2)], 'refs.json', { type: 'application/json' }));
      await apiFetch(`/api/episodes/${episodeId}/upload-shots`, { method: 'POST', body: shotsForm });

      // Upload prompt kit
      const kitForm = new FormData();
      kitForm.append('prompt_kit_file', new File([JSON.stringify(draft.prompt_kit_json, null, 2)], 'prompt_kit.json', { type: 'application/json' }));
      await apiFetch(`/api/episodes/${episodeId}/upload-prompt-kit`, { method: 'POST', body: kitForm });

      if (draft.audio_plan_json) {
        const withVoices = applyVoiceChoices(draft.audio_plan_json, voiceSlots, voiceChoices);
        const audioPlan = textOnScreen ? applyOnScreenText(withVoices, textOnScreen, draftSeconds) : withVoices;
        const audioForm = new FormData();
        audioForm.append('audio_plan_file', new File([JSON.stringify(audioPlan, null, 2)], 'audio_plan.json', { type: 'application/json' }));
        await apiFetch(`/api/episodes/${episodeId}/upload-audio-plan`, { method: 'POST', body: audioForm });
      }

      // Start episode with story shaping enabled by default
      await postJson('/api/episodes', {
        episode_id: episodeId,
        dry_run: dryRun,
        enable_story_shaping: true,
        archetype_id: draft.archetype_id ?? selectedArchetype,
        idea: submittedIdea,
        target_duration: draft.target_duration ?? targetDuration,
      });

      // Redirect to episode page
      router.push(`/studio/episodes/${episodeId}`);
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  };

  if (step === 'loading') {
    return (
      <div className="min-h-screen bg-gray-50 dark:bg-gray-900 flex items-center justify-center">
        <div className="text-center max-w-md">
          <div className="animate-spin rounded-full h-16 w-16 border-b-2 border-black dark:border-white mx-auto mb-4"></div>
          <h2 className="text-xl font-semibold mb-2">Creating your video...</h2>
          <p className="text-gray-600 dark:text-gray-400">
            Our AI is writing the script, planning shots, and setting up characters. This takes about 10-20 seconds.
          </p>
        </div>
      </div>
    );
  }

  const errorBox = error && (
    <div role="alert" className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4 mb-6 text-sm text-red-800 dark:text-red-200">
      {error}
    </div>
  );
  const visibleGroups = CARD_GROUPS.filter(group => !templateFilter || group.template === templateFilter);
  const filterClass = (active: boolean) =>
    `px-3 py-1 rounded-full border text-sm ${
      active
        ? 'bg-black dark:bg-white text-white dark:text-black border-black dark:border-white'
        : 'border-gray-300 dark:border-gray-700 hover:border-blue-500'
    }`;

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900">
      <header className="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700">
        <div className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-4">
          <Link href="/studio" className="text-sm text-blue-600 dark:text-blue-400 hover:underline mb-2 inline-block">
            ← Studio
          </Link>
          <h1 className="text-2xl font-bold">Make Your Own Video</h1>
          <p className="text-sm text-gray-600 dark:text-gray-400">
            Create an original short video in minutes. No coding required.
          </p>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {step !== 'review' && errorBox}

        {step === 'archetype' && (
          <section>
            <h2 className="text-xl font-semibold mb-4">Step 1: Choose a Style</h2>
            <p className="text-gray-600 dark:text-gray-400 mb-6">
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
                <h3 className="text-lg font-semibold mb-3">{group.label}</h3>
                <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
                  {group.presets.map((preset) => (
                    <button
                      key={preset.id}
                      type="button"
                      onClick={() => handleArchetypeSelect(preset.id)}
                      className="text-left p-4 border-2 border-gray-200 dark:border-gray-700 rounded-lg hover:border-blue-500 dark:hover:border-blue-400 transition-colors"
                      data-testid={`preset-card-${preset.id}`}
                    >
                      <h4 className="font-semibold mb-1">{preset.name}</h4>
                      <p className="text-sm text-gray-600 dark:text-gray-400 mb-2">{preset.description}</p>
                      <div className="flex flex-wrap gap-x-2 text-xs text-gray-500">
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
              className="text-sm text-blue-600 dark:text-blue-400 hover:underline"
            >
              Skip - I'll describe my style in the next step →
            </button>
          </section>
        )}

        {step === 'idea' && (
          <section className="space-y-6">
            <div>
              <button
                type="button"
                onClick={handleChangeStyle}
                className="text-sm text-blue-600 dark:text-blue-400 hover:underline"
                data-testid="change-style"
              >
                ← Change style
              </button>
            </div>

            {selectedPreset ? (
              <div className="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg p-4" data-testid="selected-style">
                <p className="text-sm font-semibold text-blue-900 dark:text-blue-200">Style: {selectedPreset.name}</p>
                <p className="text-xs text-blue-700 dark:text-blue-300">{selectedPreset.description}</p>
                <p className="text-xs text-blue-700 dark:text-blue-300 mt-1">
                  {TEMPLATE_LABELS[selectedPreset.template] ?? selectedPreset.template} · {selectedPreset.lengthRange[0]}–{selectedPreset.lengthRange[1]}s · {formatAspect(selectedPreset.aspect)} · {soundLabel(selectedPreset)}
                </p>
              </div>
            ) : (
              <div className="bg-gray-100 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-4" data-testid="selected-style">
                <p className="text-sm font-semibold">Style: Freeform</p>
                <p className="text-xs text-gray-600 dark:text-gray-400">
                  No video type picked: describe the style in your own words. {FREEFORM_LENGTH_RANGE[0]}–{FREEFORM_LENGTH_RANGE[1]}s · 9:16.
                </p>
              </div>
            )}

            <div>
              <label className="block text-lg font-semibold mb-2">Step 2: Describe Your Video</label>
              <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
                Tell us about your story, characters, setting, and mood. A few sentences is enough.
              </p>
              <textarea
                value={idea}
                onChange={(e) => setIdea(e.target.value)}
                placeholder="Example: A curious robot explores an abandoned library filled with glowing books. It discovers a special book that comes alive with magical creatures. The mood is mysterious and wonder-filled."
                rows={6}
                className="w-full px-4 py-3 border border-gray-300 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900 text-base"
                data-testid="idea-input"
              />
            </div>

            {selectedPreset && selectedPreset.briefFields.length > 0 && (
              <fieldset className="border border-gray-200 dark:border-gray-700 rounded-lg p-4" data-testid="brief-fields">
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
                        className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900"
                        data-testid={`brief-${field}`}
                      />
                    </label>
                  ))}
                </div>
              </fieldset>
            )}

            <div className="grid sm:grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-semibold mb-2">Target Length</label>
                <select
                  value={targetDuration}
                  onChange={(e) => setTargetDuration(Number(e.target.value))}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900"
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
                <label className="block text-sm font-semibold mb-2">Style (optional)</label>
                <input
                  type="text"
                  value={stylePreset}
                  onChange={(e) => setStylePreset(e.target.value)}
                  placeholder="e.g., anime, watercolor, cinematic"
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900"
                  data-testid="style-input"
                />
              </div>
            </div>

            <div className="bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded-lg p-4 text-sm">
              <p className="font-semibold text-yellow-900 dark:text-yellow-200 mb-2">
                ✓ Original Content Only
              </p>
              <p className="text-yellow-800 dark:text-yellow-300">
                Create only original characters and stories. No copyrighted characters, real people, or brand names. 
                If your idea includes them, we'll create original alternatives.
              </p>
            </div>

            <button
              onClick={handleGenerateDraft}
              disabled={busy || !idea.trim()}
              className="w-full px-6 py-3 bg-black dark:bg-white text-white dark:text-black rounded-lg font-semibold hover:bg-gray-800 dark:hover:bg-gray-200 disabled:opacity-50 disabled:cursor-not-allowed"
              data-testid="generate-button"
            >
              Generate Video Plan →
            </button>
          </section>
        )}

        {step === 'review' && draft && (
          <section className="space-y-6">
            <div>
              <h2 className="text-xl font-semibold mb-2">Step 3: Review & Start</h2>
              <p className="text-sm text-gray-600 dark:text-gray-400">
                We've created a complete plan for your video. Review it and start production.
              </p>
            </div>

            {draft.warnings && draft.warnings.length > 0 && (
              <div className="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg p-4">
                {draft.warnings.map((warning, i) => (
                  <p key={i} className="text-sm text-blue-800 dark:text-blue-200">{warning}</p>
                ))}
              </div>
            )}

            <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
              <h3 className="font-semibold mb-3">Story Overview</h3>
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

            <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
              <h3 className="font-semibold mb-3">Characters</h3>
              <div className="space-y-2">
                {Object.entries(draft.prompt_kit_json.characters || {}).map(([code, char]: [string, any]) => (
                  <div key={code} className="text-sm">
                    <span className="font-medium">{code}:</span> {char.description}
                  </div>
                ))}
              </div>
            </div>

            {voiceSlots.length > 0 && (
              <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6" data-testid="voices-section">
                <h3 className="font-semibold mb-1">Voices</h3>
                <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
                  Describe each voice in plain words and we pick a matching one. Play it, and try another until it fits.
                </p>
                <div className="space-y-4">
                  {voiceSlots.map(slot => (
                    <VoiceSlotPicker
                      key={slot.key}
                      slot={slot}
                      state={voices[slot.key] ?? emptyVoice()}
                      onDescription={(text) => patchVoice(slot.key, { description: text })}
                      onFind={() => findVoice(slot, false)}
                      onRetry={() => findVoice(slot, true)}
                    />
                  ))}
                </div>
                {draft.default_voice_available && (
                  <p className="text-xs text-gray-500 mt-4">Lines without a chosen voice use the studio's default voice.</p>
                )}
              </div>
            )}

            {textOnScreen && (
              <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6" data-testid="on-screen-text">
                <h3 className="font-semibold mb-1">Text on screen</h3>
                <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
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
                      className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900 text-sm"
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
                      className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900 text-sm"
                      data-testid="ost-end-text"
                    />
                  )}
                </div>
              </div>
            )}

            <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
              <h3 className="font-semibold mb-3">Shot List</h3>
              <div className="space-y-3">
                {draft.shots_json.shots.map((shot: any, i: number) => (
                  <div key={shot.id} className="border-l-4 border-blue-500 pl-4 py-2">
                    <p className="font-semibold text-sm">Shot {i + 1}: {shot.id}</p>
                    <p className="text-sm text-gray-600 dark:text-gray-400">{shot.prompt}</p>
                    <p className="text-xs text-gray-500 mt-1">{shot.duration}s · {shot.model}</p>
                  </div>
                ))}
              </div>
            </div>

            <div className="space-y-4">
              <button
                onClick={handleRegenerateDraft}
                disabled={busy}
                className="w-full px-6 py-3 border-2 border-gray-300 dark:border-gray-700 rounded-lg font-semibold hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
                data-testid="regenerate-button"
              >
                Regenerate Plan
              </button>

              <div className="border-t border-gray-200 dark:border-gray-700 pt-4">
                {errorBox}
                {voiceBlock && (
                  <p
                    className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg p-3 mb-4 text-sm text-amber-900 dark:text-amber-200"
                    data-testid="voice-block"
                  >
                    {voiceBlock}
                  </p>
                )}
                {textBlock && (
                  <p
                    className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg p-3 mb-4 text-sm text-amber-900 dark:text-amber-200"
                    data-testid="text-block"
                  >
                    {textBlock}
                  </p>
                )}
                <p className="text-sm font-semibold mb-3">Choose how to proceed:</p>
                
                <div className="space-y-3">
                  {liveAvailable ? (
                    <button
                      onClick={() => setShowLiveConfirm(true)}
                      disabled={busy || !!startBlock}
                      className="w-full px-6 py-3 bg-black dark:bg-white text-white dark:text-black rounded-lg font-semibold hover:bg-gray-800 dark:hover:bg-gray-200 disabled:opacity-50 text-left"
                      data-testid="make-video-button"
                    >
                      <div className="flex items-center justify-between">
                        <span>Make My Video (uses credits) →</span>
                        {draft.cost_estimate && (
                          <span className="text-sm">{formatUsd(draft.cost_estimate.total_usd_micros)}</span>
                        )}
                      </div>
                    </button>
                  ) : (
                    <div className="w-full px-6 py-3 border-2 border-gray-300 dark:border-gray-700 rounded-lg bg-gray-50 dark:bg-gray-800/50 opacity-60">
                      <div className="text-sm text-gray-600 dark:text-gray-400">
                        <p className="font-semibold mb-1">Live mode not available</p>
                        <p className="text-xs">Server is in dry-run mode. Use preview to test.</p>
                      </div>
                    </div>
                  )}

                  <button
                    onClick={() => handleStartEpisode(true)}
                    disabled={busy || !!startBlock}
                    className="w-full px-6 py-3 border-2 border-gray-300 dark:border-gray-700 rounded-lg font-semibold hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
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
              <div className="fixed inset-0 bg-black/50 flex items-center justify-center p-4 z-50">
                <div className="bg-white dark:bg-gray-800 rounded-lg p-6 max-w-md w-full">
                  <h3 className="text-lg font-semibold mb-3">Confirm Live Production</h3>
                  <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
                    This will start a live production run that uses real credits and charges you.
                  </p>
                  {draft.cost_estimate && (
                    <div className="bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded p-3 mb-4">
                      <p className="text-sm font-semibold text-yellow-900 dark:text-yellow-200">
                        Estimated cost: {formatUsd(draft.cost_estimate.total_usd_micros)}
                      </p>
                      <p className="text-xs text-yellow-800 dark:text-yellow-300 mt-1">
                        This is an estimate. Actual cost may vary based on retries and model usage.
                      </p>
                    </div>
                  )}
                  <div className="flex gap-3">
                    <button
                      onClick={() => setShowLiveConfirm(false)}
                      className="flex-1 px-4 py-2 border-2 border-gray-300 dark:border-gray-700 rounded-lg font-semibold hover:bg-gray-50 dark:hover:bg-gray-800"
                    >
                      Cancel
                    </button>
                    <button
                      onClick={() => {
                        setShowLiveConfirm(false);
                        handleStartEpisode(false);
                      }}
                      className="flex-1 px-4 py-2 bg-black dark:bg-white text-white dark:text-black rounded-lg font-semibold hover:bg-gray-800 dark:hover:bg-gray-200"
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
          <Link href="/studio/episodes/new" className="text-sm text-gray-600 dark:text-gray-400 hover:underline">
            Advanced: Upload files manually →
          </Link>
        </div>
      </main>
    </div>
  );
}

export default function CreateVideoPage() {
  return (
    <Suspense fallback={
      <div className="min-h-screen bg-gray-50 dark:bg-gray-900 flex items-center justify-center">
        <div className="text-center">
          <div className="animate-spin rounded-full h-16 w-16 border-b-2 border-black dark:border-white mx-auto mb-4"></div>
          <p className="text-gray-600 dark:text-gray-400">Loading...</p>
        </div>
      </div>
    }>
      <CreateVideoWizard />
    </Suspense>
  );
}
