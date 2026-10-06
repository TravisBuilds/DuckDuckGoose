'use client';

import { useState } from 'react';

interface Clip {
  shot_id: string;
  clip_url: string;
  qc_results: any;
  status: string;
}

interface ClipReviewProps {
  episodeId: string;
  clips: Clip[];
  onApprove: (shotId: string, verdict: 'approve' | 'reject') => Promise<void>;
}

export default function ClipReview({ episodeId, clips, onApprove }: ClipReviewProps) {
  const [selected, setSelected] = useState<string | null>(null);
  const [processing, setProcessing] = useState<string | null>(null);

  async function handleApprove(shotId: string, verdict: 'approve' | 'reject') {
    setProcessing(shotId);
    try {
      await onApprove(shotId, verdict);
    } finally {
      setProcessing(null);
    }
  }

  return (
    <div className="bg-white rounded-lg border border-gray-200 p-6">
      <h3 className="text-lg font-semibold text-gray-900 mb-4">
        Clip Review (G4.10 Motion + Duck Identity)
      </h3>
      
      {clips.length === 0 ? (
        <p className="text-gray-500 text-sm">No clips generated yet</p>
      ) : (
        <div className="space-y-4">
          {clips.map((clip) => (
            <div
              key={clip.shot_id}
              className={`border rounded-lg p-4 cursor-pointer ${
                selected === clip.shot_id
                  ? 'border-blue-500 bg-blue-50'
                  : 'border-gray-200'
              }`}
              onClick={() => setSelected(clip.shot_id)}
            >
              <div className="flex gap-4">
                <div className="w-32">
                  <div className="aspect-[9/16] bg-gray-100 rounded overflow-hidden">
                    {clip.clip_url && clip.clip_url !== '/fake/clip.mp4' ? (
                      <video
                        src={clip.clip_url}
                        className="w-full h-full object-cover"
                        controls={selected === clip.shot_id}
                      />
                    ) : (
                      <div className="w-full h-full flex items-center justify-center text-xs text-gray-400">
                        {clip.shot_id}
                      </div>
                    )}
                  </div>
                </div>

                <div className="flex-1">
                  <h4 className="font-semibold text-gray-900 mb-2">{clip.shot_id}</h4>
                  
                  {clip.qc_results && (
                    <div className="space-y-1 mb-3">
                      {clip.qc_results.motion && (
                        <div className="flex items-center gap-2 text-sm">
                          <span className={`w-2 h-2 rounded-full ${
                            clip.qc_results.motion === 'PASS'
                              ? 'bg-green-500'
                              : 'bg-red-500'
                          }`} />
                          <span className="text-gray-600">G4.10 Motion:</span>
                          <span className="font-medium">{clip.qc_results.motion}</span>
                          {clip.qc_results.peak_fd && (
                            <span className="text-xs text-gray-500">
                              (peak: {clip.qc_results.peak_fd.toFixed(2)}, 
                               mean: {clip.qc_results.mean_fd?.toFixed(2) || 'N/A'})
                            </span>
                          )}
                        </div>
                      )}
                      
                      {clip.qc_results.duck_identity && (
                        <div className="flex items-center gap-2 text-sm">
                          <span className={`w-2 h-2 rounded-full ${
                            clip.qc_results.duck_identity === 'PASS'
                              ? 'bg-green-500'
                              : clip.qc_results.duck_identity === 'FAIL'
                              ? 'bg-red-500'
                              : 'bg-yellow-500'
                          }`} />
                          <span className="text-gray-600">Duck Identity:</span>
                          <span className="font-medium">{clip.qc_results.duck_identity}</span>
                        </div>
                      )}
                    </div>
                  )}

                  {selected === clip.shot_id && (
                    <div className="flex gap-2 mt-2">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          handleApprove(clip.shot_id, 'approve');
                        }}
                        disabled={processing === clip.shot_id}
                        className="px-3 py-1 bg-green-600 text-white text-sm rounded hover:bg-green-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
                      >
                        {processing === clip.shot_id ? 'Processing...' : 'Approve'}
                      </button>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          handleApprove(clip.shot_id, 'reject');
                        }}
                        disabled={processing === clip.shot_id}
                        className="px-3 py-1 bg-red-600 text-white text-sm rounded hover:bg-red-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
                      >
                        Reject
                      </button>
                    </div>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
