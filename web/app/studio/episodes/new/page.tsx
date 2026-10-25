'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';

interface PreflightResult {
  ok: boolean;
  shots: Record<string, { ok: boolean; errors: string[]; warnings: string[] }>;
  estimated_cost_usd?: string;
  estimated_credits?: number;
}

export default function NewEpisodePage() {
  const router = useRouter();
  const [episodeId, setEpisodeId] = useState('');
  const [shotsJson, setShotsJson] = useState('');
  const [refsFiles, setRefsFiles] = useState<FileList | null>(null);
  const [validating, setValidating] = useState(false);
  const [starting, setStarting] = useState(false);
  const [preflightResult, setPreflightResult] = useState<PreflightResult | null>(null);
  const [preflightError, setPreflightError] = useState<string | null>(null);

  const handleValidate = async () => {
    if (!episodeId.match(/^ep\d{2}$/)) {
      alert('Episode ID must match pattern ep## (e.g., ep05)');
      return;
    }

    if (!shotsJson.trim()) {
      alert('Please paste shots.json content');
      return;
    }

    setValidating(true);
    setPreflightError(null);
    setPreflightResult(null);

    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
      
      // Upload shots.json and refs
      const formData = new FormData();
      formData.append('episode_id', episodeId);
      formData.append('shots_json', shotsJson);
      
      if (refsFiles) {
        for (let i = 0; i < refsFiles.length; i++) {
          formData.append('refs', refsFiles[i]);
        }
      }

      const uploadResponse = await fetch(`${apiUrl}/api/episodes/upload`, {
        method: 'POST',
        credentials: 'include',
        body: formData,
      });

      if (!uploadResponse.ok) {
        const error = await uploadResponse.json().catch(() => ({ detail: 'Upload failed' }));
        throw new Error(error.detail || 'Upload failed');
      }

      // Run preflight validation
      const preflightResponse = await fetch(`${apiUrl}/api/episodes/${episodeId}/preflight`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });

      if (!preflightResponse.ok) {
        const error = await preflightResponse.json().catch(() => ({ detail: 'Preflight failed' }));
        throw new Error(error.detail || 'Preflight failed');
      }

      const preflightData = await preflightResponse.json();
      setPreflightResult(preflightData);

    } catch (err) {
      setPreflightError(err instanceof Error ? err.message : 'Validation failed');
    } finally {
      setValidating(false);
    }
  };

  const handleStart = async () => {
    if (!preflightResult?.ok) {
      alert('Cannot start episode with preflight errors. Fix errors and validate again.');
      return;
    }

    if (!confirm(`Start episode ${episodeId}? This will begin the workflow.`)) {
      return;
    }

    setStarting(true);

    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
      
      const response = await fetch(`${apiUrl}/api/episodes`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          episode_id: episodeId,
          dry_run: true,  // Default to dry run for safety
        }),
      });

      if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: 'Start failed' }));
        throw new Error(error.detail || 'Start failed');
      }

      const result = await response.json();
      alert(`Episode ${episodeId} started! Workflow ID: ${result.workflow_id}`);
      router.push(`/studio/episodes/${episodeId}`);

    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to start episode');
    } finally {
      setStarting(false);
    }
  };

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900">
      <header className="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4">
          <div className="flex items-center justify-between">
            <div>
              <Link href="/studio" className="text-sm text-blue-600 dark:text-blue-400 hover:underline mb-2 inline-block">
                ← Studio
              </Link>
              <h1 className="text-2xl font-bold">Start New Episode</h1>
              <p className="text-sm text-gray-600 dark:text-gray-400">
                Upload shots and references, validate, then start workflow
              </p>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 mb-6">
          <h2 className="text-lg font-semibold mb-4">Episode Configuration</h2>
          
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-semibold mb-2">
                Episode ID
              </label>
              <input
                type="text"
                value={episodeId}
                onChange={(e) => setEpisodeId(e.target.value)}
                placeholder="ep05"
                pattern="ep\d{2}"
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900"
              />
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                Format: ep## (e.g., ep05)
              </p>
            </div>

            <div>
              <label className="block text-sm font-semibold mb-2">
                shots.json Content
              </label>
              <textarea
                value={shotsJson}
                onChange={(e) => setShotsJson(e.target.value)}
                placeholder='Paste shots.json content here...'
                rows={10}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg font-mono text-sm bg-white dark:bg-gray-900"
              />
            </div>

            <div>
              <label className="block text-sm font-semibold mb-2">
                Reference Images (optional)
              </label>
              <input
                type="file"
                multiple
                accept="image/*"
                onChange={(e) => setRefsFiles(e.target.files)}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg"
              />
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                Upload reference images referenced in shots.json
              </p>
            </div>
          </div>
        </div>

        {/* Preflight Results */}
        {preflightResult && (
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 mb-6">
            <h2 className="text-lg font-semibold mb-4">Validation Results</h2>
            
            <div className={`p-4 rounded-lg mb-4 ${
              preflightResult.ok
                ? 'bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800'
                : 'bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800'
            }`}>
              <p className={`font-semibold ${
                preflightResult.ok
                  ? 'text-green-800 dark:text-green-200'
                  : 'text-red-800 dark:text-red-200'
              }`}>
                {preflightResult.ok ? '✓ All shots pass validation' : '✗ Some shots have errors'}
              </p>
              
              {preflightResult.estimated_cost_usd && (
                <p className="text-sm mt-2 text-gray-700 dark:text-gray-300">
                  Estimated cost: <strong>{preflightResult.estimated_cost_usd}</strong>
                  {preflightResult.estimated_credits && (
                    <span className="ml-2">({preflightResult.estimated_credits} credits)</span>
                  )}
                </p>
              )}
            </div>

            {preflightResult.shots && (
              <div className="space-y-2">
                {Object.entries(preflightResult.shots).map(([shotId, shot]) => (
                  <div
                    key={shotId}
                    className={`p-3 rounded-lg border ${
                      shot.ok
                        ? 'bg-gray-50 dark:bg-gray-900 border-gray-200 dark:border-gray-700'
                        : 'bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-800'
                    }`}
                  >
                    <div className="font-semibold text-sm">
                      {shot.ok ? '✓' : '✗'} {shotId}
                    </div>
                    {shot.errors.map((error, i) => (
                      <div key={i} className="text-xs text-red-700 dark:text-red-300 mt-1">
                        {error}
                      </div>
                    ))}
                    {shot.warnings.map((warning, i) => (
                      <div key={i} className="text-xs text-yellow-700 dark:text-yellow-300 mt-1">
                        ⚠ {warning}
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {preflightError && (
          <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4 mb-6">
            <p className="text-red-800 dark:text-red-200 font-semibold">Validation Error</p>
            <p className="text-sm text-red-700 dark:text-red-300 mt-1">{preflightError}</p>
          </div>
        )}

        {/* Actions */}
        <div className="flex gap-3">
          <button
            onClick={handleValidate}
            disabled={validating || starting}
            className="px-6 py-3 bg-blue-600 text-white rounded-lg font-semibold hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {validating ? 'Validating...' : 'Validate'}
          </button>
          
          <button
            onClick={handleStart}
            disabled={!preflightResult?.ok || starting || validating}
            className="px-6 py-3 bg-black dark:bg-white text-white dark:text-black rounded-lg font-semibold hover:bg-gray-800 dark:hover:bg-gray-200 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {starting ? 'Starting...' : 'Start Episode'}
          </button>
        </div>

        <div className="mt-6 p-4 bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded-lg">
          <h3 className="font-semibold text-yellow-900 dark:text-yellow-200 mb-2">
            ⚠️ Before Starting
          </h3>
          <ul className="text-sm text-yellow-800 dark:text-yellow-300 space-y-1">
            <li>• Episodes start in dry-run mode by default (no charges)</li>
            <li>• Validate checks model limits and prompt compliance</li>
            <li>• Cost estimates are shown before starting</li>
            <li>• You can switch to live mode after G1.08 approval</li>
          </ul>
        </div>
      </main>
    </div>
  );
}
