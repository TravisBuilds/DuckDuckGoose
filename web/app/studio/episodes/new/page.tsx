'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { formatUsd } from '@/lib/money';
import { ApiError, EPISODE_ID_RE, apiFetch, postJson } from '@/lib/studio-api';

interface ShotPreflight { ok: boolean; errors: string[]; warnings: string[] }
interface CostEstimate {
  total_usd_micros: number;
  per_shot: Record<string, { stills_usd_micros: number; clip_usd_micros: number | null }>;
  unpriced: string[];
  source: string;
}
interface PreflightReport {
  shots: Record<string, ShotPreflight>;
  errors: { message?: string }[];
  has_errors: boolean;
  cost_estimate?: CostEstimate;
}
interface LintReport { status: 'pass' | 'fail'; issues: string[]; warnings: string[] }

type Source = { file: File | null; text: string };
const empty: Source = { file: null, text: '' };

async function sourceText(src: Source): Promise<string> {
  return src.file ? await src.file.text() : src.text;
}

function asFile(name: string, text: string, type: string): File {
  return new File([text], name, { type });
}

/** Generic new-episode flow: upload the private episode files, validate them server-side, then start. */
export default function NewEpisodePage() {
  const router = useRouter();
  const [episodeId, setEpisodeId] = useState('');
  const [beatmap, setBeatmap] = useState<Source>(empty);
  const [shots, setShots] = useState<Source>(empty);
  const [refs, setRefs] = useState<Source>(empty);
  const [promptKit, setPromptKit] = useState<Source>(empty);
  const [dryRun, setDryRun] = useState(true);
  const [busy, setBusy] = useState<'validate' | 'start' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lint, setLint] = useState<LintReport | null>(null);
  const [preflight, setPreflight] = useState<PreflightReport | null>(null);
  const [validatedFor, setValidatedFor] = useState<string | null>(null);

  const invalidate = () => { setPreflight(null); setLint(null); setValidatedFor(null); };

  const handleValidate = async () => {
    setError(null);
    invalidate();
    if (!EPISODE_ID_RE.test(episodeId)) { setError('Episode id must look like ep05 (ep + two digits).'); return; }
    setBusy('validate');
    try {
      const beatmapText = await sourceText(beatmap);
      const shotsText = await sourceText(shots);
      const refsText = await sourceText(refs);
      const kitText = await sourceText(promptKit);
      if (!beatmapText.trim()) throw new Error('BEATMAP.md is required (upload or paste it).');
      if (!shotsText.trim()) throw new Error('shots.json is required (upload or paste it).');
      try { JSON.parse(shotsText); } catch (e) { throw new Error(`shots.json is not valid JSON: ${(e as Error).message}`); }
      if (!refsText.trim()) throw new Error('refs.json is required (the workflow preflight refuses to run without it).');
      try { JSON.parse(refsText); } catch (e) { throw new Error(`refs.json is not valid JSON: ${(e as Error).message}`); }
      if (!kitText.trim()) throw new Error('prompt_kit.json is required (the workflow preflight refuses to run without it).');
      try { JSON.parse(kitText); } catch (e) { throw new Error(`prompt_kit.json is not valid JSON: ${(e as Error).message}`); }

      const beatmapForm = new FormData();
      beatmapForm.append('episode_id', episodeId);
      beatmapForm.append('beatmap_file', asFile('BEATMAP.md', beatmapText, 'text/markdown'));
      await apiFetch('/api/episodes/upload', { method: 'POST', body: beatmapForm });

      const shotsForm = new FormData();
      shotsForm.append('shots_file', asFile('shots.json', shotsText, 'application/json'));
      shotsForm.append('refs_file', asFile('refs.json', refsText, 'application/json'));
      await apiFetch(`/api/episodes/${episodeId}/upload-shots`, { method: 'POST', body: shotsForm });

      const kitForm = new FormData();
      kitForm.append('prompt_kit_file', asFile('prompt_kit.json', kitText, 'application/json'));
      await apiFetch(`/api/episodes/${episodeId}/upload-prompt-kit`, { method: 'POST', body: kitForm });

      const lintReport = await apiFetch<LintReport>(`/api/episodes/${episodeId}/lint`, { method: 'POST' });
      const report = await postJson<PreflightReport>(`/api/episodes/${episodeId}/preflight`, {});
      setLint(lintReport);
      setPreflight(report);
      setValidatedFor(episodeId);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const canStart = !!preflight && !!lint && !preflight.has_errors && lint.status === 'pass' && validatedFor === episodeId;

  const handleStart = async () => {
    if (!canStart) return;
    setBusy('start');
    setError(null);
    try {
      await postJson('/api/episodes', { episode_id: episodeId, dry_run: dryRun });
      router.push(`/studio/episodes/${episodeId}`);
    } catch (e) {
      setError(e instanceof ApiError ? `Start refused (${e.status}): ${e.message}` : String(e));
      setBusy(null);
    }
  };

  const fileInput = (label: string, testid: string, src: Source, set: (s: Source) => void, accept: string, rows: number) => (
    <div>
      <label className="block text-sm font-semibold mb-2">{label}</label>
      <input
        type="file"
        accept={accept}
        data-testid={`${testid}-file`}
        onChange={(e) => { set({ file: e.target.files?.[0] ?? null, text: '' }); invalidate(); }}
        className="block mb-2 text-sm"
      />
      <textarea
        data-testid={`${testid}-text`}
        value={src.text}
        disabled={!!src.file}
        onChange={(e) => { set({ file: null, text: e.target.value }); invalidate(); }}
        placeholder={src.file ? `Using uploaded file ${src.file.name}` : 'or paste the content here'}
        rows={rows}
        className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg font-mono text-xs bg-white dark:bg-gray-900"
      />
    </div>
  );

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900">
      <header className="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700">
        <div className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-4">
          <Link href="/studio" className="text-sm text-blue-600 dark:text-blue-400 hover:underline mb-2 inline-block">← Studio</Link>
          <h1 className="text-2xl font-bold">New Episode</h1>
          <p className="text-sm text-gray-600 dark:text-gray-400">
            Upload the episode files, validate them against the model limits, then start the workflow.
          </p>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-6">
        <section className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 space-y-4">
          <div>
            <label className="block text-sm font-semibold mb-2" htmlFor="episode-id">Episode id</label>
            <input
              id="episode-id"
              data-testid="episode-id"
              value={episodeId}
              onChange={(e) => { setEpisodeId(e.target.value.trim()); invalidate(); }}
              placeholder="ep05"
              className="w-40 px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900"
            />
            <p className="text-xs text-gray-500 mt-1">Format: ep + two digits.</p>
          </div>
          {fileInput('BEATMAP.md (required)', 'beatmap', beatmap, setBeatmap, '.md,text/markdown,text/plain', 6)}
          {fileInput('shots.json (required)', 'shots', shots, setShots, '.json,application/json', 10)}
          {fileInput('refs.json (required)', 'refs', refs, setRefs, '.json,application/json', 4)}
          {fileInput('prompt_kit.json (required: style + characters)', 'prompt_kit', promptKit, setPromptKit, '.json,application/json', 4)}
        </section>

        {error && (
          <div role="alert" data-testid="new-episode-error" className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4 text-sm text-red-800 dark:text-red-200">
            {error}
          </div>
        )}

        {lint && (
          <section data-testid="lint-report" className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
            <h2 className="text-lg font-semibold mb-2">Episode lint: {lint.status === 'pass' ? 'pass' : 'fail'}</h2>
            {lint.issues.map((m, i) => <p key={`i${i}`} className="text-sm text-red-700 dark:text-red-300">✗ {m}</p>)}
            {lint.warnings.map((m, i) => <p key={`w${i}`} className="text-sm text-yellow-700 dark:text-yellow-300">⚠ {m}</p>)}
          </section>
        )}

        {preflight && (
          <section data-testid="preflight-report" className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
            <h2 className="text-lg font-semibold mb-2" data-testid="preflight-verdict">
              {preflight.has_errors ? 'Preflight: errors (fix them and validate again)' : 'Preflight: all shots pass'}
            </h2>
            {preflight.cost_estimate && (
              <p className="text-sm mb-3" data-testid="cost-estimate">
                Estimated cost (first attempt): <strong>{formatUsd(preflight.cost_estimate.total_usd_micros)}</strong>
                <span className="text-xs text-gray-500 ml-2">{preflight.cost_estimate.source}</span>
                {preflight.cost_estimate.unpriced.length > 0 && (
                  <span className="block text-xs text-yellow-700">Unpriced: {preflight.cost_estimate.unpriced.join('; ')}</span>
                )}
              </p>
            )}
            <ul className="space-y-1">
              {Object.entries(preflight.shots).map(([sid, shot]) => (
                <li key={sid} data-testid={`preflight-shot-${sid}`} className="text-sm">
                  <span className={shot.ok ? 'text-green-700' : 'text-red-700'}>{shot.ok ? '✓' : '✗'} {sid}</span>
                  {preflight.cost_estimate?.per_shot[sid] && (
                    <span className="text-xs text-gray-500 ml-2">
                      stills {formatUsd(preflight.cost_estimate.per_shot[sid].stills_usd_micros)}
                      {preflight.cost_estimate.per_shot[sid].clip_usd_micros !== null &&
                        `, clip ${formatUsd(preflight.cost_estimate.per_shot[sid].clip_usd_micros as number)}`}
                    </span>
                  )}
                  {shot.errors.map((m, i) => <div key={`e${i}`} className="text-xs text-red-700 ml-4">{m}</div>)}
                  {shot.warnings.map((m, i) => <div key={`w${i}`} className="text-xs text-yellow-700 ml-4">⚠ {m}</div>)}
                </li>
              ))}
            </ul>
          </section>
        )}

        <section className="flex flex-wrap items-center gap-4">
          <button
            data-testid="validate-button"
            onClick={handleValidate}
            disabled={busy !== null}
            className="px-6 py-3 bg-blue-600 text-white rounded-lg font-semibold hover:bg-blue-700 disabled:opacity-50"
          >
            {busy === 'validate' ? 'Validating…' : 'Upload & validate'}
          </button>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" data-testid="dry-run" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} />
            Dry run (no provider calls, no spend)
          </label>
          <button
            data-testid="start-button"
            onClick={handleStart}
            disabled={!canStart || busy !== null}
            className="px-6 py-3 bg-black dark:bg-white text-white dark:text-black rounded-lg font-semibold disabled:opacity-50"
          >
            {busy === 'start' ? 'Starting…' : dryRun ? 'Start dry run' : 'Start live run'}
          </button>
        </section>
        <p className="text-xs text-gray-500">
          Start stays disabled until lint and preflight pass for the files just uploaded; the API refuses to start
          an episode whose uploaded shots.json fails validation or preflight in any case.
        </p>
      </main>
    </div>
  );
}
