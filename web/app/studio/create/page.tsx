'use client';

import React, { Suspense, useState } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { formatUsd } from '@/lib/money';
import { ApiError, apiFetch, postJson } from '@/lib/studio-api';
import { presets, getPresetById } from '@/lib/presets';

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
  preset_used?: {
    id: string;
    name: string;
    template: string;
    aspect: string;
    vo_mode: string;
  };
}

type WizardStep = 'archetype' | 'idea' | 'review' | 'loading';

function CreateVideoWizard() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const preselectedArchetype = searchParams?.get('archetype');

  const [step, setStep] = useState<WizardStep>(preselectedArchetype ? 'idea' : 'archetype');
  const [selectedArchetype, setSelectedArchetype] = useState<string | null>(preselectedArchetype);
  const [idea, setIdea] = useState('');
  const [targetDuration, setTargetDuration] = useState(60);
  const [stylePreset, setStylePreset] = useState('');
  const [draft, setDraft] = useState<DraftResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showLiveConfirm, setShowLiveConfirm] = useState(false);
  const [liveAvailable, setLiveAvailable] = useState(false);

  const selectedPreset = selectedArchetype ? getPresetById(selectedArchetype) : null;

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
    setSelectedArchetype(archetypeId);
    setStep('idea');
  };

  const handleGenerateDraft = async () => {
    if (!idea.trim()) {
      setError('Please describe your video idea');
      return;
    }

    setError(null);
    setBusy(true);
    setStep('loading');

    try {
      const response = await postJson<DraftResponse>('/api/episodes/draft-from-idea', {
        idea: idea.trim(),
        archetype_id: selectedArchetype,
        target_duration: targetDuration,
        style_preset: stylePreset || null,
      });

      setDraft(response);
      setStep('review');
    } catch (e) {
      const errorMsg = e instanceof ApiError 
        ? e.message 
        : e instanceof Error 
        ? e.message 
        : String(e);
      setError(errorMsg);
      setStep('idea');
    } finally {
      setBusy(false);
    }
  };

  const handleRegenerateDraft = async () => {
    await handleGenerateDraft();
  };

  const handleStartEpisode = async (dryRun: boolean) => {
    if (!draft) return;

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

      // Upload audio plan
      const audioForm = new FormData();
      audioForm.append('audio_plan_file', new File([JSON.stringify(draft.audio_plan_json, null, 2)], 'audio_plan.json', { type: 'application/json' }));
      await apiFetch(`/api/episodes/${episodeId}/upload-audio-plan`, { method: 'POST', body: audioForm });

      // Start episode with story shaping enabled by default
      await postJson('/api/episodes', {
        episode_id: episodeId,
        dry_run: dryRun,
        enable_story_shaping: true,
        archetype_id: selectedArchetype,
        idea: idea.trim(),
      });

      // Redirect to episode page
      router.push(`/studio/episodes/${episodeId}`);
    } catch (e) {
      const errorMsg = e instanceof ApiError 
        ? e.message 
        : e instanceof Error 
        ? e.message 
        : String(e);
      setError(errorMsg);
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
        {error && (
          <div role="alert" className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4 mb-6 text-sm text-red-800 dark:text-red-200">
            {error}
          </div>
        )}

        {step === 'archetype' && (
          <section>
            <h2 className="text-xl font-semibold mb-4">Step 1: Choose a Style</h2>
            <p className="text-gray-600 dark:text-gray-400 mb-6">
              Pick a genre or style that fits your story. You can customize everything later.
            </p>
            
            <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4 mb-6">
              {presets.map((preset) => (
                <button
                  key={preset.id}
                  onClick={() => handleArchetypeSelect(preset.id)}
                  className="text-left p-4 border-2 border-gray-200 dark:border-gray-700 rounded-lg hover:border-blue-500 dark:hover:border-blue-400 transition-colors"
                >
                  <h3 className="font-semibold mb-1">{preset.name}</h3>
                  <p className="text-sm text-gray-600 dark:text-gray-400 mb-2">{preset.description}</p>
                  <span className="text-xs text-gray-500">{preset.lengthRange[0]}–{preset.lengthRange[1]}s</span>
                </button>
              ))}
            </div>

            <button
              onClick={() => setStep('idea')}
              className="text-sm text-blue-600 dark:text-blue-400 hover:underline"
            >
              Skip - I'll describe my style in the next step →
            </button>
          </section>
        )}

        {step === 'idea' && !draft && (
          <section className="space-y-6">
            <div>
              <Link href="/studio/create" className="text-sm text-blue-600 dark:text-blue-400 hover:underline">
                ← Change style
              </Link>
            </div>

            {selectedPreset && (
              <div className="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg p-4">
                <p className="text-sm font-semibold text-blue-900 dark:text-blue-200">Style: {selectedPreset.name}</p>
                <p className="text-xs text-blue-700 dark:text-blue-300">{selectedPreset.description}</p>
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

            <div className="grid sm:grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-semibold mb-2">Target Length</label>
                <select
                  value={targetDuration}
                  onChange={(e) => setTargetDuration(Number(e.target.value))}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900"
                  data-testid="duration-select"
                >
                  <option value={30}>30 seconds (quick)</option>
                  <option value={60}>60 seconds (recommended)</option>
                  <option value={120}>2 minutes (full story)</option>
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
                <p className="text-sm font-semibold mb-3">Choose how to proceed:</p>
                
                <div className="space-y-3">
                  {liveAvailable ? (
                    <button
                      onClick={() => setShowLiveConfirm(true)}
                      disabled={busy}
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
                    disabled={busy}
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
