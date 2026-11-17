'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { ApiError, postJson } from '@/lib/studio-api';
import { getPresetById, presets } from '@/lib/presets';
import { groupByTemplate } from '@/lib/wizard.mjs';
import { btnPrimary, input as field } from '@/lib/ui';

const CARD_GROUPS = groupByTemplate(presets);
const NAME_MAX = 60;
const LOGLINE_MAX = 200;

/**
 * Start a Universe: its name, a one-line premise and the kind of video its episodes default to
 * (POST /api/worlds; nothing is generated or charged). Then its page opens on the next step: the
 * references for the mood board.
 */
export function UniverseCreateForm({ heading = 'h2' }: { heading?: 'h1' | 'h2' }) {
  const router = useRouter();
  const [name, setName] = useState('');
  const [logline, setLogline] = useState('');
  const [presetId, setPresetId] = useState('');
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const preset = getPresetById(presetId);
  const Title = heading;

  const create = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const made = await postJson<{ world: { series_id: string } }>('/api/worlds', {
        name: name.trim(), logline: logline.trim(), preset_id: presetId || null,
      });
      router.push(`/studio/universes/${encodeURIComponent(made.world.series_id)}?created=1`);
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : 'The studio could not be reached. Check your connection and try again.');
      setBusy(false);
    }
  };

  return (
    <form data-testid="universe-create-form" onSubmit={(e) => { e.preventDefault(); if (name.trim()) create(); }}>
      <Title className="text-xl font-semibold text-gray-900 dark:text-white">Create a Universe</Title>
      <p className="mt-1 text-sm text-gray-700 dark:text-gray-200">
        A Universe holds the characters, places and look your episodes share. Name it now; next you add
        photos and notes, the studio makes a mood board from them, and once you approve it, writes the
        characters and places for you.
      </p>
      <label htmlFor="universe-name" className="mt-4 block text-sm font-medium">Name</label>
      <input id="universe-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={NAME_MAX}
        placeholder="e.g. The Lighthouse Kids" className={`${field} mt-1`} data-testid="universe-name" />
      <label htmlFor="universe-logline" className="mt-3 block text-sm font-medium">In one line (optional)</label>
      <input id="universe-logline" value={logline} onChange={(e) => setLogline(e.target.value)} maxLength={LOGLINE_MAX}
        placeholder="e.g. Three friends keep an old lighthouse running on a stormy island"
        className={`${field} mt-1`} data-testid="universe-logline" />
      <label htmlFor="universe-genre" className="mt-3 block text-sm font-medium">Kind of video (optional)</label>
      <select id="universe-genre" value={presetId} onChange={(e) => setPresetId(e.target.value)}
        className={`${field} mt-1`} data-testid="universe-genre">
        <option value="">Decide per episode</option>
        {CARD_GROUPS.map((group) => (
          <optgroup key={group.template} label={group.label}>
            {group.presets.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </optgroup>
        ))}
      </select>
      {preset && (
        <p className="mt-1 text-xs text-gray-600 dark:text-gray-300" data-testid="universe-genre-hint">
          {preset.description}. New episodes in this Universe start with this card; you can change it per episode.
        </p>
      )}
      {problem && <p role="alert" className="mt-3 text-sm text-red-700 dark:text-red-300" data-testid="universe-create-problem">{problem}</p>}
      <button type="submit" disabled={busy || !name.trim()} className={`${btnPrimary} mt-4`}
        data-testid="universe-create">
        {busy ? 'Creating…' : 'Create the Universe'}
      </button>
    </form>
  );
}

export default UniverseCreateForm;
