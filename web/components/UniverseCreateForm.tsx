'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { ApiError, postJson } from '@/lib/studio-api';
import { getPresetById, presets } from '@/lib/presets';
import { groupByTemplate } from '@/lib/wizard.mjs';
import { input as field, label } from '@/lib/ui';

const CARD_GROUPS = groupByTemplate(presets);
const NAME_MAX = 60;
const LOGLINE_MAX = 200;
const STEPS: [string, string][] = [
  ['Spark', 'bg-coral'], ['References', 'bg-mustard'], ['Board', 'bg-blue'], ['Bible', 'bg-aqua'], ['Lock', 'bg-green'],
];

/**
 * Start a Universe (the Spark step): its name, what it is about and the kind of video its episodes
 * default to (POST /api/worlds; nothing is generated or charged). Then the creation route opens on
 * the next step: references for the mood board.
 */
export function UniverseCreateForm({ heading = 'h2' }: { heading?: 'h1' | 'h2' | 'h3' }) {
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
      router.push(`/studio/universes/${encodeURIComponent(made.world.series_id)}/create?step=references`);
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : 'The studio could not be reached. Check your connection and try again.');
      setBusy(false);
    }
  };

  return (
    <form data-testid="universe-create-form" onSubmit={(e) => { e.preventDefault(); if (name.trim()) create(); }}>
      <Title className={heading === 'h1' ? 'text-[32px]' : 'text-[22px]'}>Start a new Universe</Title>
      <p className="mt-0.5 text-[13px] text-ink-soft">Five friendly steps. You see the price before anything costs money.</p>
      <ol aria-label="The five steps" className="mb-4 mt-2.5 flex gap-1.5">
        {STEPS.map(([step, colour]) => (
          <li key={step} className="flex-1 text-center text-[11px] font-extrabold text-ink-soft">
            <span aria-hidden="true" className={`mb-1 block h-1.5 rounded-full ${colour}`} />{step}
          </li>
        ))}
      </ol>
      <label htmlFor="universe-name" className={label}>Name</label>
      <input id="universe-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={NAME_MAX}
        placeholder="e.g. Copper Hollow" className={field} data-testid="universe-name" />
      <label htmlFor="universe-logline" className={`${label} mt-3`}>What&apos;s it about?</label>
      <textarea id="universe-logline" value={logline} onChange={(e) => setLogline(e.target.value)} maxLength={LOGLINE_MAX} rows={2}
        placeholder="e.g. A kiln village where every bell remembers a name…" className={`${field} resize-none`}
        data-testid="universe-logline" />
      <label htmlFor="universe-genre" className={`${label} mt-3`}>Kind of video (optional)</label>
      <select id="universe-genre" value={presetId} onChange={(e) => setPresetId(e.target.value)}
        className={field} data-testid="universe-genre">
        <option value="">Decide per episode</option>
        {CARD_GROUPS.map((group) => (
          <optgroup key={group.template} label={group.label}>
            {group.presets.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </optgroup>
        ))}
      </select>
      {preset && (
        <p className="mt-1 text-[12px] text-ink-soft" data-testid="universe-genre-hint">
          {preset.description}. New episodes in this Universe start with this card; you can change it per episode.
        </p>
      )}
      {problem && <p role="alert" className="mt-3 text-[13px] text-coral-ink" data-testid="universe-create-problem">{problem}</p>}
      <button type="submit" disabled={busy || !name.trim()} className="pf-btn pf-btn-primary mt-4 w-full"
        title={name.trim() ? undefined : 'Turns on once your Universe has a name.'}
        aria-describedby={name.trim() ? undefined : 'universe-create-why'} data-testid="universe-create">
        {busy ? 'Creating…' : 'Start gathering references'}
      </button>
      {!name.trim() && (
        <p id="universe-create-why" className="pf-off-why text-center" data-testid="universe-create-why">
          Turns on once your Universe has a name.
        </p>
      )}
    </form>
  );
}

export default UniverseCreateForm;
