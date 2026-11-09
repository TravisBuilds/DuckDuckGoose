'use client';

import { useState } from 'react';

interface StoryBeat {
  beat_id: string;
  description: string;
  emotional_tone: string;
  target_duration_s: number;
  shot_ids: string[];
}

interface StoryShape {
  logline: string;
  hook: string;
  hook_deadline_s: number;
  beats: StoryBeat[];
  midpoint_beat_id: string;
  payoff: string;
  payoff_references_hook: boolean;
  total_duration_s: number;
}

interface StoryShapeCritique {
  score: number;
  issues: string[];
  suggestions: string[];
  passes: boolean;
}

interface StoryShapeData {
  episode_id: string;
  story_shape: StoryShape | null;
  critique: StoryShapeCritique | null;
  approved: boolean;
}

interface StoryShapePanelProps {
  episodeId: string;
  data: StoryShapeData | null;
  onApprove: () => Promise<void>;
  onUpdate: (shape: StoryShape) => Promise<void>;
}

export default function StoryShapePanel({
  episodeId,
  data,
  onApprove,
  onUpdate,
}: StoryShapePanelProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [editedShape, setEditedShape] = useState<StoryShape | null>(null);
  const [loading, setLoading] = useState(false);

  if (!data || !data.story_shape) {
    return (
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6">
        <h2 className="text-xl font-semibold mb-4">Story Shape</h2>
        <p className="text-gray-500">Story shaping not enabled for this episode.</p>
      </div>
    );
  }

  const { story_shape, critique, approved } = data;

  const handleEdit = () => {
    setEditedShape(story_shape);
    setIsEditing(true);
  };

  const handleSave = async () => {
    if (!editedShape) return;
    
    setLoading(true);
    try {
      await onUpdate(editedShape);
      setIsEditing(false);
    } catch (error) {
      console.error('Failed to update story shape:', error);
      alert('Failed to update story shape');
    } finally {
      setLoading(false);
    }
  };

  const handleCancel = () => {
    setEditedShape(null);
    setIsEditing(false);
  };

  const handleApprove = async () => {
    setLoading(true);
    try {
      await onApprove();
    } catch (error) {
      console.error('Failed to approve story shape:', error);
      alert('Failed to approve story shape');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg shadow">
      <div className="p-6 border-b border-gray-200 dark:border-gray-700">
        <div className="flex items-center justify-between">
          <h2 className="text-xl font-semibold">Story Shape</h2>
          <div className="flex gap-2">
            {approved ? (
              <span className="px-3 py-1 bg-green-100 text-green-800 rounded-full text-sm font-medium">
                Approved
              </span>
            ) : (
              <>
                {!isEditing && (
                  <button
                    onClick={handleEdit}
                    className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded hover:bg-gray-200"
                    disabled={loading}
                  >
                    Edit
                  </button>
                )}
                <button
                  onClick={handleApprove}
                  className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded hover:bg-blue-700"
                  disabled={loading || isEditing}
                >
                  Approve
                </button>
              </>
            )}
          </div>
        </div>
      </div>

      <div className="p-6 space-y-6">
        {/* Logline */}
        <div>
          <h3 className="text-sm font-medium text-gray-500 mb-2">Logline</h3>
          {isEditing ? (
            <input
              type="text"
              value={editedShape?.logline || ''}
              onChange={(e) =>
                setEditedShape((prev) => (prev ? { ...prev, logline: e.target.value } : null))
              }
              className="w-full px-3 py-2 border border-gray-300 rounded focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            />
          ) : (
            <p className="text-lg">{story_shape.logline}</p>
          )}
        </div>

        {/* Hook */}
        <div>
          <h3 className="text-sm font-medium text-gray-500 mb-2">Hook ({story_shape.hook_deadline_s}s)</h3>
          {isEditing ? (
            <textarea
              value={editedShape?.hook || ''}
              onChange={(e) =>
                setEditedShape((prev) => (prev ? { ...prev, hook: e.target.value } : null))
              }
              rows={2}
              className="w-full px-3 py-2 border border-gray-300 rounded focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            />
          ) : (
            <p>{story_shape.hook}</p>
          )}
        </div>

        {/* Beats */}
        <div>
          <h3 className="text-sm font-medium text-gray-500 mb-3">
            Story Beats ({story_shape.beats.length})
          </h3>
          <div className="space-y-3">
            {story_shape.beats.map((beat, idx) => (
              <div
                key={beat.beat_id}
                className={`p-4 rounded-lg border ${
                  beat.beat_id === story_shape.midpoint_beat_id
                    ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20'
                    : 'border-gray-200 dark:border-gray-700'
                }`}
              >
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-3">
                    <span className="text-sm font-medium text-gray-500">#{idx + 1}</span>
                    <span className="text-sm font-semibold">{beat.beat_id}</span>
                    {beat.beat_id === story_shape.midpoint_beat_id && (
                      <span className="px-2 py-1 bg-blue-100 text-blue-800 text-xs rounded">
                        Midpoint
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-3 text-sm text-gray-500">
                    <span>{beat.target_duration_s}s</span>
                    <span className="capitalize">{beat.emotional_tone}</span>
                  </div>
                </div>
                <p className="text-sm">{beat.description}</p>
                {beat.shot_ids.length > 0 && (
                  <div className="mt-2 text-xs text-gray-500">
                    Shots: {beat.shot_ids.join(', ')}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>

        {/* Payoff */}
        <div>
          <h3 className="text-sm font-medium text-gray-500 mb-2">Payoff</h3>
          {isEditing ? (
            <textarea
              value={editedShape?.payoff || ''}
              onChange={(e) =>
                setEditedShape((prev) => (prev ? { ...prev, payoff: e.target.value } : null))
              }
              rows={2}
              className="w-full px-3 py-2 border border-gray-300 rounded focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            />
          ) : (
            <p>{story_shape.payoff}</p>
          )}
        </div>

        {/* Critique */}
        {critique && (
          <div className="border-t border-gray-200 dark:border-gray-700 pt-6">
            <h3 className="text-sm font-medium text-gray-500 mb-3">Critique</h3>
            <div className="space-y-3">
              <div className="flex items-center gap-3">
                <span className="text-sm font-medium">Score:</span>
                <span
                  className={`px-3 py-1 rounded-full text-sm font-medium ${
                    critique.score >= 80
                      ? 'bg-green-100 text-green-800'
                      : critique.score >= 60
                      ? 'bg-yellow-100 text-yellow-800'
                      : 'bg-red-100 text-red-800'
                  }`}
                >
                  {critique.score}/100
                </span>
                {critique.passes ? (
                  <span className="text-green-600 text-sm">✓ Passes</span>
                ) : (
                  <span className="text-red-600 text-sm">✗ Needs revision</span>
                )}
              </div>

              {critique.issues.length > 0 && (
                <div>
                  <h4 className="text-sm font-medium text-red-600 mb-2">Issues:</h4>
                  <ul className="list-disc list-inside space-y-1 text-sm">
                    {critique.issues.map((issue, idx) => (
                      <li key={idx}>{issue}</li>
                    ))}
                  </ul>
                </div>
              )}

              {critique.suggestions.length > 0 && (
                <div>
                  <h4 className="text-sm font-medium text-blue-600 mb-2">Suggestions:</h4>
                  <ul className="list-disc list-inside space-y-1 text-sm">
                    {critique.suggestions.map((suggestion, idx) => (
                      <li key={idx}>{suggestion}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          </div>
        )}

        {/* Edit Actions */}
        {isEditing && (
          <div className="flex justify-end gap-2 pt-4 border-t border-gray-200 dark:border-gray-700">
            <button
              onClick={handleCancel}
              className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded hover:bg-gray-200"
              disabled={loading}
            >
              Cancel
            </button>
            <button
              onClick={handleSave}
              className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded hover:bg-blue-700"
              disabled={loading}
            >
              {loading ? 'Saving...' : 'Save Changes'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
