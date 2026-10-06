'use client';

import { useEffect, useState } from 'react';
import { use } from 'react';
import Link from 'next/link';

interface EpisodeState {
  episode_id: string;
  stage: string | null;
  approvals: Record<string, boolean>;
  shots_count: number;
}

interface BudgetLine {
  line_name: string;
  provider: string;
  spent: number;
  reserved: number;
  total: number;
  cap: number;
  stop: number;
  at_stop: boolean;
  unit: string;
}

interface BudgetStatus {
  episode_id: string;
  lines: BudgetLine[];
  higgsfield_total: number;
  elevenlabs_total: number;
}

const APPROVAL_GATES = [
  { id: 'g101', name: 'G1.01: Pitch pick', step: 'Story' },
  { id: 'g103', name: 'G1.03: Beatmap approval', step: 'Story' },
  { id: 'g108', name: 'G1.08: Credit plan approval', step: 'Story' },
  { id: 'gc02', name: 'GC.02: Budget tracking', step: 'Story' },
  { id: 'g201', name: 'G2.01: New refs approval', step: 'Stills' },
  { id: 'g212', name: 'G2.12: Still strip approval', step: 'Stills' },
  { id: 'g406', name: 'G4.06: Cut-for-story review', step: 'Mute' },
  { id: 'g408', name: 'G4.08: Mute notes logged', step: 'Mute' },
  { id: 'g409', name: 'G4.09: Picture lock', step: 'Mute' },
];

export default function EpisodePage({ params }: { params: Promise<{ id: string }> }) {
  const { id: episodeId } = use(params);
  
  const [state, setState] = useState<EpisodeState | null>(null);
  const [budget, setBudget] = useState<BudgetStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [approving, setApproving] = useState<string | null>(null);

  useEffect(() => {
    loadEpisodeData();
    const interval = setInterval(loadEpisodeData, 3000);
    return () => clearInterval(interval);
  }, [episodeId]);

  const getAdminSecret = () => {
    return document.cookie
      .split('; ')
      .find(row => row.startsWith('studio_admin_token='))
      ?.split('=')[1];
  };

  const loadEpisodeData = async () => {
    try {
      const secret = getAdminSecret();
      if (!secret) return;

      const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
      const headers = { 'Authorization': `Bearer ${secret}` };

      const [stateRes, budgetRes] = await Promise.all([
        fetch(`${apiUrl}/api/episodes/${episodeId}`, { headers }),
        fetch(`${apiUrl}/api/episodes/${episodeId}/budget`, { headers }),
      ]);

      if (!stateRes.ok) throw new Error('Failed to load episode state');
      if (!budgetRes.ok) throw new Error('Failed to load budget');

      setState(await stateRes.json());
      setBudget(await budgetRes.json());
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load episode');
    } finally {
      setLoading(false);
    }
  };

  const handleApprove = async (gateId: string) => {
    setApproving(gateId);
    try {
      const secret = getAdminSecret();
      if (!secret) throw new Error('Not authenticated');

      const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
      
      const response = await fetch(`${apiUrl}/api/episodes/${episodeId}/approve`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${secret}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ gate_id: gateId }),
      });

      if (!response.ok) throw new Error('Approval failed');

      // Reload data
      await loadEpisodeData();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Approval failed');
    } finally {
      setApproving(null);
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-center">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-black dark:border-white mx-auto mb-4"></div>
          <p>Loading episode {episodeId}...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-center">
          <p className="text-red-600 dark:text-red-400 mb-4">{error}</p>
          <Link href="/studio" className="text-sm text-blue-600 dark:text-blue-400 hover:underline">
            ← Back to Studio
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900">
      <header className="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4">
          <div className="flex items-center justify-between">
            <div>
              <Link href="/studio" className="text-sm text-blue-600 dark:text-blue-400 hover:underline mb-2 inline-block">
                ← Studio
              </Link>
              <h1 className="text-2xl font-bold">Episode {episodeId.toUpperCase()}</h1>
              <p className="text-sm text-gray-600 dark:text-gray-400">
                Stage: {state?.stage || 'Unknown'} • {state?.shots_count || 0} shots
              </p>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Budget Overview */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 mb-6">
          <h2 className="text-lg font-semibold mb-4">Budget Status</h2>
          
          <div className="grid md:grid-cols-2 gap-4 mb-6">
            <div>
              <div className="text-sm text-gray-600 dark:text-gray-400 mb-1">
                Higgsfield Total
              </div>
              <div className="text-2xl font-bold">
                {budget?.higgsfield_total.toFixed(1) || '0.0'}
                <span className="text-sm font-normal text-gray-600 dark:text-gray-400 ml-2">
                  / 1,250 cap
                </span>
              </div>
              <div className="mt-2 w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2">
                <div
                  className="bg-blue-600 h-2 rounded-full"
                  style={{ width: `${Math.min((budget?.higgsfield_total || 0) / 1250 * 100, 100)}%` }}
                />
              </div>
            </div>
            
            <div>
              <div className="text-sm text-gray-600 dark:text-gray-400 mb-1">
                ElevenLabs Total (deferred)
              </div>
              <div className="text-2xl font-bold text-gray-400 dark:text-gray-600">
                {budget?.elevenlabs_total.toFixed(1) || '0.0'}
              </div>
              <div className="text-sm text-gray-500 dark:text-gray-400">
                Audio after picture lock
              </div>
            </div>
          </div>

          <div className="space-y-2">
            {budget?.lines.map((line) => (
              <div
                key={line.line_name}
                className={`p-3 rounded-lg border ${
                  line.at_stop
                    ? 'bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-800'
                    : 'bg-gray-50 dark:bg-gray-900 border-gray-200 dark:border-gray-700'
                }`}
              >
                <div className="flex justify-between items-center mb-2">
                  <div className="font-medium text-sm">{line.line_name}</div>
                  <div className="text-sm">
                    {line.total.toFixed(1)} / {line.stop.toFixed(1)}
                    <span className="text-gray-500 dark:text-gray-400 ml-1">
                      (cap {line.cap.toFixed(1)})
                    </span>
                  </div>
                </div>
                <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-1.5">
                  <div
                    className={`h-1.5 rounded-full ${
                      line.at_stop ? 'bg-red-600' : 'bg-green-600'
                    }`}
                    style={{ width: `${Math.min((line.total / line.stop) * 100, 100)}%` }}
                  />
                </div>
                <div className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  Spent: {line.spent.toFixed(1)} • Reserved: {line.reserved.toFixed(1)}
                  {line.at_stop && <span className="ml-2 text-red-600 dark:text-red-400 font-semibold">⚠️ AT STOP</span>}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Approval Gates */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 mb-6">
          <h2 className="text-lg font-semibold mb-4">Travis Approval Gates</h2>
          
          <div className="space-y-2">
            {APPROVAL_GATES.map((gate) => {
              const isApproved = state?.approvals[gate.id] || false;
              
              return (
                <div
                  key={gate.id}
                  className={`p-4 rounded-lg border flex items-center justify-between ${
                    isApproved
                      ? 'bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-800'
                      : 'bg-gray-50 dark:bg-gray-900 border-gray-200 dark:border-gray-700'
                  }`}
                >
                  <div>
                    <div className="font-medium">{gate.name}</div>
                    <div className="text-sm text-gray-600 dark:text-gray-400">
                      Step: {gate.step}
                    </div>
                  </div>
                  
                  {isApproved ? (
                    <div className="flex items-center gap-2 text-green-600 dark:text-green-400">
                      <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                        <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
                      </svg>
                      <span className="font-semibold">Approved</span>
                    </div>
                  ) : (
                    <button
                      onClick={() => handleApprove(gate.id)}
                      disabled={approving === gate.id}
                      className="px-4 py-2 bg-black dark:bg-white text-white dark:text-black rounded-lg text-sm font-semibold hover:bg-gray-800 dark:hover:bg-gray-200 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {approving === gate.id ? 'Approving...' : 'Approve'}
                    </button>
                  )}
                </div>
              );
            })}
          </div>

          <div className="mt-4 p-4 bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded-lg">
            <p className="text-sm text-yellow-800 dark:text-yellow-300">
              <strong>G4.09 Picture Lock</strong> ends the video phase. Sound (VO, music, SFX, mix) is deferred to a later slice.
            </p>
          </div>
        </div>

        {/* Shot List */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h2 className="text-lg font-semibold mb-4">Shot List</h2>
          <div className="text-sm text-gray-600 dark:text-gray-400">
            <p>Shot tracking UI will be implemented after workflow integration.</p>
            <p className="mt-2">Expected features:</p>
            <ul className="list-disc list-inside mt-2 space-y-1">
              <li>Shot status (prompt → still → clip → QC)</li>
              <li>Still strip review with QC verdicts</li>
              <li>Clip review with G4.10 motion check results</li>
              <li>Duck identity gate results (PASS/FAIL/ESCALATE)</li>
              <li>Retry tracking</li>
            </ul>
          </div>
        </div>
      </main>
    </div>
  );
}
