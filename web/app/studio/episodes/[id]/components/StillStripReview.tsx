'use client';

import { useState } from 'react';

interface Still {
  shot_id: string;
  still_url: string;
  qc_results: any;
  status: string;
}

interface StillStripReviewProps {
  episodeId: string;
  stills: Still[];
  onApprove: (shotId: string, verdict: 'approve' | 'reject') => Promise<void>;
}

export default function StillStripReview({ episodeId, stills, onApprove }: StillStripReviewProps) {
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
        Still Strip Review (G2.12)
      </h3>
      
      {stills.length === 0 ? (
        <p className="text-gray-500 text-sm">No stills generated yet</p>
      ) : (
        <div className="space-y-4">
          <div className="grid grid-cols-5 gap-2">
            {stills.map((still) => (
              <div
                key={still.shot_id}
                className={`relative cursor-pointer border-2 rounded ${
                  selected === still.shot_id
                    ? 'border-blue-500'
                    : 'border-gray-200'
                }`}
                onClick={() => setSelected(still.shot_id)}
              >
                <div className="aspect-[9/16] bg-gray-100">
                  {still.still_url && still.still_url !== '/fake/still.jpg' ? (
                    <img
                      src={still.still_url}
                      alt={still.shot_id}
                      className="w-full h-full object-cover"
                    />
                  ) : (
                    <div className="w-full h-full flex items-center justify-center text-xs text-gray-400">
                      {still.shot_id}
                    </div>
                  )}
                </div>
                
                <div className="absolute top-1 left-1 bg-black/60 text-white text-xs px-1 rounded">
                  {still.shot_id}
                </div>
                
                {still.qc_results?.duck_identity && (
                  <div className={`absolute top-1 right-1 w-2 h-2 rounded-full ${
                    still.qc_results.duck_identity === 'PASS'
                      ? 'bg-green-500'
                      : still.qc_results.duck_identity === 'FAIL'
                      ? 'bg-red-500'
                      : 'bg-yellow-500'
                  }`} />
                )}
              </div>
            ))}
          </div>

          {selected && (
            <div className="border-t pt-4">
              {(() => {
                const still = stills.find(s => s.shot_id === selected);
                if (!still) return null;

                return (
                  <div className="space-y-4">
                    <div className="flex gap-4">
                      <div className="w-48">
                        <div className="aspect-[9/16] bg-gray-100 rounded overflow-hidden">
                          {still.still_url && still.still_url !== '/fake/still.jpg' ? (
                            <img
                              src={still.still_url}
                              alt={still.shot_id}
                              className="w-full h-full object-cover"
                            />
                          ) : (
                            <div className="w-full h-full flex items-center justify-center text-gray-400">
                              Preview
                            </div>
                          )}
                        </div>
                      </div>

                      <div className="flex-1 space-y-3">
                        <div>
                          <h4 className="font-semibold text-gray-900">{still.shot_id}</h4>
                          <p className="text-sm text-gray-600">Status: {still.status}</p>
                        </div>

                        {still.qc_results && (
                          <div className="space-y-2">
                            <p className="text-sm font-medium text-gray-700">QC Results:</p>
                            {Object.entries(still.qc_results).map(([gate, verdict]) => (
                              <div key={gate} className="flex items-center gap-2 text-sm">
                                <span className={`w-2 h-2 rounded-full ${
                                  verdict === 'PASS'
                                    ? 'bg-green-500'
                                    : verdict === 'FAIL'
                                    ? 'bg-red-500'
                                    : 'bg-yellow-500'
                                }`} />
                                <span className="text-gray-600">{gate}:</span>
                                <span className="font-medium">{String(verdict)}</span>
                              </div>
                            ))}
                          </div>
                        )}

                        <div className="flex gap-2 pt-2">
                          <button
                            onClick={() => handleApprove(still.shot_id, 'approve')}
                            disabled={processing === still.shot_id}
                            className="px-4 py-2 bg-green-600 text-white rounded hover:bg-green-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
                          >
                            {processing === still.shot_id ? 'Processing...' : 'Approve'}
                          </button>
                          <button
                            onClick={() => handleApprove(still.shot_id, 'reject')}
                            disabled={processing === still.shot_id}
                            className="px-4 py-2 bg-red-600 text-white rounded hover:bg-red-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
                          >
                            Reject
                          </button>
                        </div>
                      </div>
                    </div>
                  </div>
                );
              })()}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
