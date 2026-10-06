'use client';

import { useEffect, useState } from 'react';

interface Shot {
  id: string;
  shot_id: string;
  prompt: string;
  still_url: string | null;
  clip_url: string | null;
  status: string;
  retries: number;
  qc_results: any;
}

interface ShotsListProps {
  episodeId: string;
}

export default function ShotsList({ episodeId }: ShotsListProps) {
  const [shots, setShots] = useState<Shot[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    loadShots();
    
    const interval = setInterval(loadShots, 5000);
    return () => clearInterval(interval);
  }, [episodeId]);

  async function loadShots() {
    try {
      const response = await fetch(`/api/studio/episodes/${episodeId}/shots`);
      if (response.ok) {
        const data = await response.json();
        setShots(data);
      }
    } catch (error) {
      console.error('Failed to load shots:', error);
    } finally {
      setLoading(false);
    }
  }

  if (loading) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 p-6">
        <p className="text-gray-500">Loading shots...</p>
      </div>
    );
  }

  if (shots.length === 0) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 p-6">
        <p className="text-gray-500">No shots yet. Upload BEATMAP.md to begin.</p>
      </div>
    );
  }

  return (
    <div className="bg-white rounded-lg border border-gray-200 p-6">
      <h3 className="text-lg font-semibold text-gray-900 mb-4">
        Shots ({shots.length})
      </h3>
      
      <div className="space-y-4">
        {shots.map((shot) => (
          <div
            key={shot.id}
            className="border border-gray-200 rounded-lg p-4"
          >
            <div className="flex items-start justify-between mb-2">
              <div>
                <span className="font-mono text-sm font-semibold text-gray-900">
                  {shot.shot_id}
                </span>
                <span className={`ml-3 px-2 py-1 rounded text-xs font-medium ${
                  shot.status === 'complete' ? 'bg-green-100 text-green-800' :
                  shot.status === 'in_progress' ? 'bg-blue-100 text-blue-800' :
                  shot.status === 'failed' ? 'bg-red-100 text-red-800' :
                  'bg-gray-100 text-gray-800'
                }`}>
                  {shot.status}
                </span>
              </div>
              
              {shot.retries > 0 && (
                <span className="text-xs text-gray-500">
                  {shot.retries} {shot.retries === 1 ? 'retry' : 'retries'}
                </span>
              )}
            </div>

            {shot.prompt && (
              <p className="text-sm text-gray-600 mb-3 line-clamp-2">
                {shot.prompt}
              </p>
            )}

            <div className="grid grid-cols-2 gap-4">
              <div>
                <p className="text-xs font-medium text-gray-700 mb-1">Still</p>
                {shot.still_url ? (
                  <div className="relative aspect-[9/16] bg-gray-100 rounded overflow-hidden">
                    <img
                      src={shot.still_url}
                      alt={`${shot.shot_id} still`}
                      className="w-full h-full object-cover"
                    />
                  </div>
                ) : (
                  <div className="aspect-[9/16] bg-gray-50 rounded flex items-center justify-center text-xs text-gray-400">
                    No still
                  </div>
                )}
              </div>

              <div>
                <p className="text-xs font-medium text-gray-700 mb-1">Clip</p>
                {shot.clip_url ? (
                  <div className="relative aspect-[9/16] bg-gray-100 rounded overflow-hidden">
                    <video
                      src={shot.clip_url}
                      className="w-full h-full object-cover"
                      controls
                    />
                  </div>
                ) : (
                  <div className="aspect-[9/16] bg-gray-50 rounded flex items-center justify-center text-xs text-gray-400">
                    No clip
                  </div>
                )}
              </div>
            </div>

            {shot.qc_results && (
              <div className="mt-3 pt-3 border-t border-gray-200">
                <p className="text-xs font-medium text-gray-700 mb-1">QC Results</p>
                <pre className="text-xs text-gray-600 bg-gray-50 rounded p-2 overflow-x-auto">
                  {JSON.stringify(shot.qc_results, null, 2)}
                </pre>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
