'use client';

import { Suspense, use, useState } from 'react';
import EpisodeUpload from './components/EpisodeUpload';
import ShotsList from './components/ShotsList';
import StillStripReview from './components/StillStripReview';
import ClipReview from './components/ClipReview';
import AuditTrail from './components/AuditTrail';

interface PageProps {
  params: Promise<{ id: string }>;
}

export default function EpisodeDetailPage({ params }: PageProps) {
  const { id } = use(params);
  const [refreshKey, setRefreshKey] = useState(0);

  async function handleApprove(shotId: string, verdict: 'approve' | 'reject') {
    await fetch(`/api/studio/episodes/${id}/shots/${shotId}/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ verdict }),
    });
    setRefreshKey(k => k + 1);
  }

  async function handleRunCanary(shotId: string) {
    const response = await fetch(`/api/studio/episodes/${id}/canary`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ shot_id: shotId }),
    });
    
    if (response.ok) {
      alert('Canary started for ' + shotId);
      setRefreshKey(k => k + 1);
    } else {
      const error = await response.json();
      alert('Canary failed: ' + error.detail);
    }
  }

  return (
    <div className="max-w-7xl mx-auto py-8 px-4">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-gray-900">Episode {id}</h1>
        <p className="mt-2 text-gray-600">
          DuckDuckGoose Production Console - Video workflow up to picture lock (G4.09)
        </p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 space-y-6">
          <EpisodeUpload episodeId={id} onUploadComplete={() => setRefreshKey(k => k + 1)} />
          
          <div className="bg-white rounded-lg border border-gray-200 p-4">
            <button
              onClick={() => handleRunCanary('A01')}
              className="w-full bg-blue-600 text-white py-2 px-4 rounded hover:bg-blue-700"
            >
              Run Canary (A01: 1 still + 1 clip)
            </button>
          </div>

          <ShotsList key={refreshKey} episodeId={id} />
          
          <StillStripReview
            key={`still-${refreshKey}`}
            episodeId={id}
            stills={[]}
            onApprove={handleApprove}
          />
          
          <ClipReview
            key={`clip-${refreshKey}`}
            episodeId={id}
            clips={[]}
            onApprove={handleApprove}
          />
        </div>

        <div className="space-y-6">
          <BudgetPanel key={refreshKey} episodeId={id} />
          <ApprovalsPanel episodeId={id} />
          <AuditTrail key={refreshKey} episodeId={id} />
        </div>
      </div>
    </div>
  );
}

function BudgetPanel({ episodeId }: { episodeId: string }) {
  const [budget, setBudget] = useState<any>(null);

  useState(() => {
    fetch(`/api/studio/episodes/${episodeId}/budget`)
      .then(r => r.json())
      .then(setBudget);
  });

  if (!budget) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 p-6">
        <h3 className="text-lg font-semibold text-gray-900 mb-4">Budget</h3>
        <div className="text-sm text-gray-500">Loading...</div>
      </div>
    );
  }

  return (
    <div className="bg-white rounded-lg border border-gray-200 p-6">
      <h3 className="text-lg font-semibold text-gray-900 mb-4">Budget</h3>
      
      <div className="space-y-3 mb-4">
        {budget.lines.slice(0, 4).map((line: any) => (
          <div key={line.id} className="space-y-1">
            <div className="flex justify-between text-sm">
              <span className="font-medium text-gray-700">{line.id}</span>
              <span className="text-gray-600">
                {line.spent.toFixed(1)} / {line.stop} ({line.percent_of_stop.toFixed(0)}%)
              </span>
            </div>
            <div className="w-full bg-gray-200 rounded-full h-2">
              <div
                className={`h-2 rounded-full ${
                  line.percent_of_stop >= 100
                    ? 'bg-red-500'
                    : line.percent_of_stop >= 80
                    ? 'bg-yellow-500'
                    : 'bg-green-500'
                }`}
                style={{ width: `${Math.min(line.percent_of_stop, 100)}%` }}
              />
            </div>
          </div>
        ))}
      </div>

      <div className="pt-3 border-t text-sm">
        <div className="flex justify-between">
          <span className="font-medium">Total:</span>
          <span>{budget.totals.spent.toFixed(1)} / {budget.totals.cap} cr</span>
        </div>
      </div>
    </div>
  );
}

function ApprovalsPanel({ episodeId }: { episodeId: string }) {
  return (
    <div className="bg-white rounded-lg border border-gray-200 p-6">
      <h3 className="text-lg font-semibold text-gray-900 mb-4">Gate Approvals</h3>
      <div className="space-y-2 text-sm">
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-green-500" />
          <span>G1.01 Pitch pick</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-green-500" />
          <span>G1.03 Beat map</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-green-500" />
          <span>G1.08 Credit plan</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-gray-300" />
          <span>G2.12 Still strip</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-gray-300" />
          <span>G4.09 Picture lock</span>
        </div>
      </div>
    </div>
  );
}
