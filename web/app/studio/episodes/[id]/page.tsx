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

interface Shot {
  id: string;
  episode_id: string;
  shot_id: string;
  status: string;
  still_url: string | null;
  clip_url: string | null;
  prompt: string;
  retries: number;
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

interface Gates {
  live_mode: boolean;
  g108_approved: boolean;
}

interface AuditEntry {
  id: number;
  action: string;
  details: string;
  user: string;
  timestamp: string;
}

export default function EpisodePage({ params }: { params: Promise<{ id: string }> }) {
  const { id: episodeId } = use(params);
  
  const [state, setState] = useState<EpisodeState | null>(null);
  const [budget, setBudget] = useState<BudgetStatus | null>(null);
  const [gates, setGates] = useState<Gates | null>(null);
  const [shots, setShots] = useState<Shot[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [showUpload, setShowUpload] = useState(false);
  const [showAudit, setShowAudit] = useState(false);
  const [liveConfirmText, setLiveConfirmText] = useState('');
  const [showLiveConfirm, setShowLiveConfirm] = useState(false);
  const [canaryRunning, setCanaryRunning] = useState(false);
  const [canaryWorkflowId, setCanaryWorkflowId] = useState<string | null>(null);
  const [canaryStatus, setCanaryStatus] = useState<string | null>(null);
  const [canaryError, setCanaryError] = useState<string | null>(null);

  useEffect(() => {
    loadEpisodeData();
    const interval = setInterval(loadEpisodeData, 3000);
    return () => clearInterval(interval);
  }, [episodeId]);

  useEffect(() => {
    // Poll canary status if we have a workflow_id and it's not completed/failed
    if (canaryWorkflowId && canaryStatus !== 'completed' && canaryStatus !== 'failed') {
      const pollCanary = async () => {
        try {
          const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
          const response = await fetch(`${apiUrl}/api/canary/${canaryWorkflowId}`, {
            credentials: 'include',
          });

          if (response.ok) {
            const result = await response.json();
            setCanaryStatus(result.status);
            if (result.status === 'failed') {
              setCanaryError(result.error || 'Unknown error');
            }
            if (result.status === 'completed' || result.status === 'failed') {
              setCanaryRunning(false);
              await loadEpisodeData(); // Refresh budget after completion
            }
          }
        } catch (err) {
          console.error('Failed to poll canary status:', err);
        }
      };

      pollCanary();
      const interval = setInterval(pollCanary, 2000);
      return () => clearInterval(interval);
    }
  }, [canaryWorkflowId, canaryStatus]);



  const loadEpisodeData = async () => {
    try {
      // Session cookie is httpOnly - can't read in JS
      // Make API calls with credentials: 'include'


      const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

      const [stateRes, budgetRes, gatesRes, shotsRes, auditRes] = await Promise.all([
        fetch(`${apiUrl}/api/episodes/${episodeId}`, { credentials: 'include' }),
        fetch(`${apiUrl}/api/episodes/${episodeId}/budget`, { credentials: 'include' }),
        fetch(`${apiUrl}/api/episodes/${episodeId}/gates`, { credentials: 'include' }),
        fetch(`${apiUrl}/api/episodes/${episodeId}/shots`, { credentials: 'include' }),
        fetch(`${apiUrl}/api/episodes/${episodeId}/audit`, { credentials: 'include' }),
      ]);

      if (stateRes.ok) setState(await stateRes.json());
      if (budgetRes.ok) setBudget(await budgetRes.json());
      if (gatesRes.ok) setGates(await gatesRes.json());
      if (shotsRes.ok) setShots((await shotsRes.json()).shots || []);
      if (auditRes.ok) setAudit((await auditRes.json()).entries || []);
      
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load episode');
    } finally {
      setLoading(false);
    }
  };

  const handleUpload = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const formData = new FormData(e.currentTarget);
    
    try {
      // Use httpOnly cookie via credentials: 'include'

      const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
      
      const response = await fetch(`${apiUrl}/api/episodes/upload`, {
        method: 'POST',
        
        credentials: 'include',
        body: formData,
      });

      if (!response.ok) throw new Error('Upload failed');

      alert('Files uploaded successfully!');
      setShowUpload(false);
      await loadEpisodeData();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Upload failed');
    }
  };

  const handleApproveG108 = async () => {
    try {
      // Use httpOnly cookie via credentials: 'include'

      const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
      
      const response = await fetch(`${apiUrl}/api/episodes/${episodeId}/approve-g108`, {
        method: 'POST',
        
        credentials: 'include',
      });

      if (!response.ok) throw new Error('Approval failed');
      await loadEpisodeData();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Approval failed');
    }
  };

  const handleSetLiveMode = async () => {
    if (liveConfirmText !== 'ENABLE LIVE MODE') {
      alert('You must type "ENABLE LIVE MODE" to confirm');
      return;
    }

    try {
      // Use httpOnly cookie via credentials: 'include'

      const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
      
      const response = await fetch(`${apiUrl}/api/episodes/${episodeId}/set-live`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          confirmation: 'ENABLE_LIVE_MODE',
        }),
        credentials: 'include',
      });

      if (!response.ok) throw new Error('Failed to enable live mode');
      
      setShowLiveConfirm(false);
      setLiveConfirmText('');
      await loadEpisodeData();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to enable live mode');
    }
  };

  const handleSetDryMode = async () => {
    if (!confirm('Switch to dry mode? This will disable live generation.')) return;

    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
      
      const response = await fetch(`${apiUrl}/api/episodes/${episodeId}/set-dry`, {
        method: 'POST',
        credentials: 'include',
      });

      if (!response.ok) throw new Error('Failed to set dry mode');
      
      await loadEpisodeData();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to set dry mode');
    }
  };

  const handleRunCanary = async () => {
    if (!confirm('Run canary test? This will generate 1 still + 1 clip.')) return;

    setCanaryRunning(true);
    setCanaryStatus('starting');
    setCanaryError(null);
    try {
      // Use httpOnly cookie via credentials: 'include'

      const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
      
      const response = await fetch(`${apiUrl}/api/episodes/${episodeId}/canary`, {
        method: 'POST',
        
        credentials: 'include',
      });

      const result = await response.json();
      
      if (!result.success) {
        alert(`Canary refused: ${result.message}`);
        setCanaryRunning(false);
        setCanaryStatus(null);
        return;
      }

      // Store workflow_id to poll for status
      setCanaryWorkflowId(result.workflow_id);
      setCanaryStatus('running');
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Canary failed');
      setCanaryRunning(false);
      setCanaryStatus(null);
    }
  };

  const handleApproveStill = async (shotId: string) => {
    try {
      // Use httpOnly cookie via credentials: 'include'

      const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
      
      const response = await fetch(`${apiUrl}/api/episodes/${episodeId}/shots/${shotId}/approve`, {
        method: 'POST',
        
        credentials: 'include',
      });

      if (!response.ok) throw new Error('Approval failed');
      await loadEpisodeData();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Approval failed');
    }
  };

  const handleRejectStill = async (shotId: string) => {
    const reason = prompt('Rejection reason:');
    if (!reason) return;

    try {
      // Use httpOnly cookie via credentials: 'include'

      const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
      
      const formData = new FormData();
      formData.append('reason', reason);

      const response = await fetch(`${apiUrl}/api/episodes/${episodeId}/shots/${shotId}/reject`, {
        method: 'POST',
        
        credentials: 'include',
        body: formData,
      });

      if (!response.ok) throw new Error('Rejection failed');
      await loadEpisodeData();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Rejection failed');
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

  const estimatedCanaryCost = 6.5 + 28.0; // L1 still + L4 clip
  // Episode cap from backend budget.py:EPISODE_CAP
  // TODO: expose this value through the budget API if it changes
  const EPISODE_CAP_CREDITS = 1250;

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
                {shots.length} shots loaded
              </p>
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => setShowUpload(true)}
                className="px-4 py-2 border border-gray-300 dark:border-gray-700 rounded-lg text-sm hover:bg-gray-50 dark:hover:bg-gray-700"
              >
                Upload Files
              </button>
              <button
                onClick={() => setShowAudit(!showAudit)}
                className="px-4 py-2 border border-gray-300 dark:border-gray-700 rounded-lg text-sm hover:bg-gray-50 dark:hover:bg-gray-700"
              >
                {showAudit ? 'Hide' : 'Show'} Audit Trail
              </button>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Gates and Controls */}
        <div className="grid md:grid-cols-2 gap-6 mb-6">
          {/* Live Mode */}
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
            <h2 className="text-lg font-semibold mb-4">Live Mode</h2>
            {gates?.live_mode ? (
              <div className="flex items-center gap-2 text-green-600 dark:text-green-400 mb-4">
                <div className="w-3 h-3 bg-green-600 dark:bg-green-400 rounded-full animate-pulse"></div>
                <span className="font-semibold">LIVE - Real providers active</span>
              </div>
            ) : (
              <div className="flex items-center gap-2 text-gray-600 dark:text-gray-400 mb-4">
                <div className="w-3 h-3 bg-gray-400 rounded-full"></div>
                <span>DRY RUN - Fake providers (no spend)</span>
              </div>
            )}
            
            {!gates?.live_mode && (
              <button
                onClick={() => setShowLiveConfirm(true)}
                className="w-full px-4 py-2 bg-red-600 text-white rounded-lg font-semibold hover:bg-red-700"
              >
                Enable Live Mode
              </button>
            )}
            
            
            
            {gates?.live_mode && (
              <button
                onClick={handleSetDryMode}
                className="w-full mt-4 px-4 py-2 bg-gray-600 text-white rounded-lg font-semibold hover:bg-gray-700"
              >
                Switch to Dry Mode
              </button>
            )}
            
            {showLiveConfirm && (
              <div className="mt-4 p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg">
                <p className="text-sm text-red-800 dark:text-red-300 mb-3">
                  <strong>WARNING:</strong> Live mode will charge real credits. Type "ENABLE LIVE MODE" to confirm:
                </p>
                <input
                  type="text"
                  value={liveConfirmText}
                  onChange={(e) => setLiveConfirmText(e.target.value)}
                  className="w-full px-3 py-2 border border-red-300 dark:border-red-700 rounded-lg mb-2 bg-white dark:bg-gray-900"
                  placeholder="Type here..."
                />
                <div className="flex gap-2">
                  <button
                    onClick={handleSetLiveMode}
                    disabled={liveConfirmText !== 'ENABLE LIVE MODE'}
                    className="flex-1 px-4 py-2 bg-red-600 text-white rounded-lg font-semibold hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    Confirm
                  </button>
                  <button
                    onClick={() => {
                      setShowLiveConfirm(false);
                      setLiveConfirmText('');
                    }}
                    className="flex-1 px-4 py-2 border border-gray-300 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </div>

          {/* G1.08 & Canary */}
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
            <h2 className="text-lg font-semibold mb-4">G1.08 Credit Plan & Canary</h2>
            {gates?.g108_approved ? (
              <div className="flex items-center gap-2 text-green-600 dark:text-green-400 mb-4">
                <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                  <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
                </svg>
                <span className="font-semibold">G1.08 Approved</span>
              </div>
            ) : (
              <div>
                <p className="text-sm text-gray-600 dark:text-gray-400 mb-3">
                  Credit plan must be approved before canary or paid generation.
                </p>
                <button
                  onClick={handleApproveG108}
                  className="w-full px-4 py-2 bg-black dark:bg-white text-white dark:text-black rounded-lg font-semibold hover:bg-gray-800 dark:hover:bg-gray-200 mb-4"
                >
                  Approve G1.08
                </button>
              </div>
            )}

            {gates?.g108_approved && (
              <div className="mt-4 p-4 bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg">
                <p className="text-sm text-blue-800 dark:text-blue-300 mb-2">
                  <strong>Canary Test</strong>: 1 still + 1 clip
                </p>
                <p className="text-sm text-blue-700 dark:text-blue-400 mb-3">
                  Estimated cost: <strong>{estimatedCanaryCost.toFixed(1)} credits</strong>
                </p>
                {canaryStatus && (
                  <div className={`mb-3 p-2 rounded text-sm ${
                    canaryStatus === 'completed' ? 'bg-green-100 dark:bg-green-900/30 text-green-800 dark:text-green-300' :
                    canaryStatus === 'failed' ? 'bg-red-100 dark:bg-red-900/30 text-red-800 dark:text-red-300' :
                    canaryStatus === 'running' ? 'bg-yellow-100 dark:bg-yellow-900/30 text-yellow-800 dark:text-yellow-300' :
                    'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-300'
                  }`}>
                    Status: <strong>{canaryStatus}</strong>
                    {canaryError && <div className="mt-1 text-xs">Error: {canaryError}</div>}
                  </div>
                )}
                <button
                  onClick={handleRunCanary}
                  disabled={canaryRunning}
                  className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg font-semibold hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {canaryRunning ? 'Running...' : 'Run Canary'}
                </button>
              </div>
            )}
          </div>
        </div>

        {/* Budget Overview */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 mb-6">
          <h2 className="text-lg font-semibold mb-4">Budget Status</h2>
          
          <div className="grid md:grid-cols-2 gap-4 mb-6">
            <div>
              <div className="text-sm text-gray-600 dark:text-gray-400 mb-1">
                Higgsfield Total
              </div>
              <div className="text-2xl font-bold">
                {budget?.higgsfield_total.toFixed(1) || '0.0'} credits
                <span className="text-sm font-normal text-gray-600 dark:text-gray-400 ml-2">
                  / {EPISODE_CAP_CREDITS} credits episode cap
                </span>
              </div>
              <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                Line caps total: {budget?.lines.reduce((sum, l) => l.provider === 'higgsfield' ? sum + l.cap : sum, 0).toFixed(1) || '0'} credits
              </div>
            </div>
            
            <div>
              <div className="text-sm text-gray-600 dark:text-gray-400 mb-1">
                ElevenLabs Total (deferred)
              </div>
              <div className="text-2xl font-bold text-gray-400 dark:text-gray-600">
                {budget?.elevenlabs_total.toFixed(1) || '0.0'} credits
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
                    {line.total.toFixed(1)} / {line.stop.toFixed(1)} credits
                    <span className="text-gray-500 dark:text-gray-400 ml-1">
                      (cap {line.cap.toFixed(1)} credits)
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
                  Spent: {line.spent.toFixed(1)} • Reserved: {line.reserved.toFixed(1)} credits
                  {line.at_stop && <span className="ml-2 text-red-600 dark:text-red-400 font-semibold">⚠️ AT STOP</span>}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Shot List */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 mb-6">
          <h2 className="text-lg font-semibold mb-4">Shot List ({shots.length} shots)</h2>
          
          {shots.length === 0 ? (
            <p className="text-sm text-gray-600 dark:text-gray-400">
              No shots loaded. Upload BEATMAP.md to load shots.
            </p>
          ) : (
            <div className="space-y-4">
              {shots.map((shot) => (
                <div
                  key={shot.id}
                  className="p-4 border border-gray-200 dark:border-gray-700 rounded-lg"
                >
                  <div className="flex justify-between items-start mb-2">
                    <div>
                      <span className="font-semibold">{shot.shot_id}</span>
                      <span className={`ml-3 text-sm px-2 py-0.5 rounded ${
                        shot.status === 'completed' ? 'bg-green-100 dark:bg-green-900/30 text-green-800 dark:text-green-300' :
                        shot.status === 'running' ? 'bg-blue-100 dark:bg-blue-900/30 text-blue-800 dark:text-blue-300' :
                        shot.status === 'failed' ? 'bg-red-100 dark:bg-red-900/30 text-red-800 dark:text-red-300' :
                        'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-300'
                      }`}>
                        {shot.status}
                      </span>
                      {shot.retries > 0 && (
                        <span className="ml-2 text-xs text-gray-500">
                          (retry {shot.retries})
                        </span>
                      )}
                    </div>
                  </div>
                  
                  <p className="text-sm text-gray-600 dark:text-gray-400 mb-3">
                    {shot.prompt}
                  </p>

                  {shot.still_url && (
                    <div className="mb-3">
                      <div className="text-xs font-semibold mb-1">Still:</div>
                      <div className="flex items-center gap-2">
                        <img 
                          src={shot.still_url} 
                          alt={shot.shot_id}
                          className="w-32 h-32 object-cover rounded border border-gray-300 dark:border-gray-600"
                        />
                        <div className="flex flex-col gap-2">
                          <button
                            onClick={() => handleApproveStill(shot.shot_id)}
                            className="px-3 py-1 bg-green-600 text-white text-sm rounded hover:bg-green-700"
                          >
                            Approve Still
                          </button>
                          <button
                            onClick={() => handleRejectStill(shot.shot_id)}
                            className="px-3 py-1 bg-red-600 text-white text-sm rounded hover:bg-red-700"
                          >
                            Reject Still
                          </button>
                        </div>
                      </div>
                    </div>
                  )}

                  {shot.clip_url && (
                    <div>
                      <div className="text-xs font-semibold mb-1">Clip:</div>
                      <video 
                        src={shot.clip_url} 
                        controls
                        className="w-64 rounded border border-gray-300 dark:border-gray-600"
                      />
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Audit Trail */}
        {showAudit && (
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
            <h2 className="text-lg font-semibold mb-4">Audit Trail</h2>
            {audit.length === 0 ? (
              <p className="text-sm text-gray-600 dark:text-gray-400">No audit entries yet.</p>
            ) : (
              <div className="space-y-2">
                {audit.map((entry) => (
                  <div
                    key={entry.id}
                    className="p-3 border border-gray-200 dark:border-gray-700 rounded-lg text-sm"
                  >
                    <div className="flex justify-between items-start mb-1">
                      <span className="font-semibold">{entry.action}</span>
                      <span className="text-xs text-gray-500">
                        {new Date(entry.timestamp).toLocaleString()}
                      </span>
                    </div>
                    <p className="text-gray-600 dark:text-gray-400">{entry.details}</p>
                    <p className="text-xs text-gray-500 mt-1">User: {entry.user}</p>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </main>

      {/* Upload Modal */}
      {showUpload && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center p-4 z-50">
          <div className="bg-white dark:bg-gray-800 rounded-lg p-6 max-w-md w-full">
            <h2 className="text-xl font-bold mb-4">Upload Episode Files</h2>
            
            <form onSubmit={handleUpload} className="space-y-4">
              <input type="hidden" name="episode_id" value={episodeId} />
              
              <div>
                <label className="block text-sm font-semibold mb-1">
                  BEATMAP.md (required)
                </label>
                <input
                  type="file"
                  name="beatmap_file"
                  accept=".md"
                  required
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg"
                />
              </div>

              <div>
                <label className="block text-sm font-semibold mb-1">
                  CREDIT-PLAN.md (optional)
                </label>
                <input
                  type="file"
                  name="credit_plan_file"
                  accept=".md"
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg"
                />
              </div>

              <div>
                <label className="block text-sm font-semibold mb-1">
                  CONTINUITY.md (optional)
                </label>
                <input
                  type="file"
                  name="continuity_file"
                  accept=".md"
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg"
                />
              </div>

              <div className="flex gap-2 pt-4">
                <button
                  type="submit"
                  className="flex-1 px-4 py-2 bg-black dark:bg-white text-white dark:text-black rounded-lg font-semibold hover:bg-gray-800 dark:hover:bg-gray-200"
                >
                  Upload
                </button>
                <button
                  type="button"
                  onClick={() => setShowUpload(false)}
                  className="flex-1 px-4 py-2 border border-gray-300 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700"
                >
                  Cancel
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
