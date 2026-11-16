'use client';

import { useState } from 'react';
import { ApiError } from '@/lib/studio-api';
import { btnPrimary, btnSecondary, card, chip, focusRing, input, panel, tones } from '@/lib/ui';

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
  /** No Edit or Approve: the story is approved, the video has moved past it, or the viewer may not change it. */
  readOnly?: boolean;
  /** The story was approved (the run state's approvals; GET /story-shape's own flag lags). */
  approved?: boolean;
  /** Why the story cannot be changed, shown when read-only and not approved. */
  note?: string | null;
}

function problemText(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  return 'The studio could not be reached. Check your connection and try again.';
}

export default function StoryShapePanel({
  data,
  onApprove,
  onUpdate,
  readOnly = false,
  approved: approvedProp,
  note,
}: StoryShapePanelProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [editedShape, setEditedShape] = useState<StoryShape | null>(null);
  const [loading, setLoading] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  if (!data || !data.story_shape) {
    return (
      <div className={card}>
        <h2 className="text-xl font-semibold mb-2">Story</h2>
        <p className="text-gray-700 dark:text-gray-200">This video has no story outline.</p>
      </div>
    );
  }

  const { story_shape, critique } = data;
  const approved = Boolean(approvedProp || data.approved);
  const editable = !readOnly && !approved;

  const handleEdit = () => {
    setEditedShape(story_shape);
    setProblem(null);
    setIsEditing(true);
  };

  const handleSave = async () => {
    if (!editedShape) return;
    setLoading(true);
    setProblem(null);
    try {
      await onUpdate(editedShape);
      setIsEditing(false);
    } catch (error) {
      setProblem(`Your changes were not saved: ${problemText(error)}`);
    } finally {
      setLoading(false);
    }
  };

  const handleCancel = () => {
    setEditedShape(null);
    setProblem(null);
    setIsEditing(false);
  };

  const handleApprove = async () => {
    setLoading(true);
    setProblem(null);
    try {
      await onApprove();
    } catch (error) {
      setProblem(`The story was not approved: ${problemText(error)}`);
    } finally {
      setLoading(false);
    }
  };

  const field = `${input} mt-1`;

  return (
    <div className={panel}>
      <div className="p-4 sm:p-6 border-b border-gray-200 dark:border-gray-700">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-xl font-semibold">Story</h2>
          <div className="flex flex-wrap items-center gap-2">
            {approved && <span className={`${chip} ${tones.ok}`} data-testid="story-approved">Approved</span>}
            {!approved && readOnly && <span className={`${chip} ${tones.wait}`}>Locked</span>}
            {editable && !isEditing && (
              <button data-testid="story-edit" onClick={handleEdit} className={btnSecondary} disabled={loading}>
                Edit
              </button>
            )}
            {editable && (
              <button data-testid="story-approve" onClick={handleApprove} className={btnPrimary} disabled={loading || isEditing}>
                Approve the story
              </button>
            )}
          </div>
        </div>
        {editable && !isEditing && (
          <p className="mt-2 text-sm text-gray-700 dark:text-gray-200">
            Read it through. Edit anything that is not right, then approve it to start planning the shots.
          </p>
        )}
        {!approved && readOnly && note && <p className="mt-2 text-sm text-gray-700 dark:text-gray-200">{note}</p>}
        {problem && (
          <p role="alert" data-testid="story-problem" className="mt-3 rounded-lg bg-red-50 p-3 text-sm text-red-800 dark:bg-red-900/20 dark:text-red-200">
            {problem}
          </p>
        )}
      </div>

      <div className="p-4 sm:p-6 space-y-6">
        <div>
          {isEditing ? (
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-200">
              In one sentence
              <input data-testid="story-logline-input" type="text" value={editedShape?.logline || ''}
                onChange={(e) => setEditedShape((prev) => (prev ? { ...prev, logline: e.target.value } : null))}
                className={field} />
            </label>
          ) : (
            <>
              <h3 className="text-sm font-medium text-gray-600 dark:text-gray-300 mb-1">In one sentence</h3>
              <p className="text-lg" data-testid="story-logline">{story_shape.logline}</p>
            </>
          )}
        </div>

        <div>
          {isEditing ? (
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-200">
              The opening (first {story_shape.hook_deadline_s} seconds)
              <textarea value={editedShape?.hook || ''} rows={2}
                onChange={(e) => setEditedShape((prev) => (prev ? { ...prev, hook: e.target.value } : null))}
                className={field} />
            </label>
          ) : (
            <>
              <h3 className="text-sm font-medium text-gray-600 dark:text-gray-300 mb-1">
                The opening (first {story_shape.hook_deadline_s} seconds)
              </h3>
              <p>{story_shape.hook}</p>
            </>
          )}
        </div>

        <div>
          <h3 className="text-sm font-medium text-gray-600 dark:text-gray-300 mb-3">
            What happens ({story_shape.beats.length} parts)
          </h3>
          <ol className="space-y-3">
            {story_shape.beats.map((beat, idx) => (
              <li key={beat.beat_id}
                className={`p-4 rounded-lg border ${
                  beat.beat_id === story_shape.midpoint_beat_id
                    ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20'
                    : 'border-gray-200 dark:border-gray-700'
                }`}>
                <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
                  <div className="flex items-center gap-3">
                    <span className="text-sm font-semibold text-gray-700 dark:text-gray-200">Part {idx + 1}</span>
                    {beat.beat_id === story_shape.midpoint_beat_id && (
                      <span className={`${chip} ${tones.todo}`}>The turning point</span>
                    )}
                  </div>
                  <div className="flex items-center gap-3 text-sm text-gray-600 dark:text-gray-300">
                    <span>{beat.target_duration_s} s</span>
                    <span className="capitalize">{beat.emotional_tone}</span>
                  </div>
                </div>
                <p className="text-sm">{beat.description}</p>
                {beat.shot_ids.length > 0 && (
                  <p className="mt-2 text-xs text-gray-600 dark:text-gray-300">Shots: {beat.shot_ids.join(', ')}</p>
                )}
              </li>
            ))}
          </ol>
        </div>

        <div>
          {isEditing ? (
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-200">
              The ending
              <textarea value={editedShape?.payoff || ''} rows={2}
                onChange={(e) => setEditedShape((prev) => (prev ? { ...prev, payoff: e.target.value } : null))}
                className={field} />
            </label>
          ) : (
            <>
              <h3 className="text-sm font-medium text-gray-600 dark:text-gray-300 mb-1">The ending</h3>
              <p>{story_shape.payoff}</p>
            </>
          )}
        </div>

        {critique && (
          <details className="border-t border-gray-200 dark:border-gray-700 pt-4">
            <summary className={`cursor-pointer rounded text-sm font-medium text-gray-700 dark:text-gray-200 ${focusRing}`}>
              What the story checker said ({critique.score}/100{critique.passes ? ', looks good' : ', worth another look'})
            </summary>
            <div className="mt-3 space-y-3">
              {critique.issues.length > 0 && (
                <div>
                  <h4 className="text-sm font-medium text-red-700 dark:text-red-300 mb-1">Things to watch</h4>
                  <ul className="list-disc list-inside space-y-1 text-sm">
                    {critique.issues.map((issue, idx) => <li key={idx}>{issue}</li>)}
                  </ul>
                </div>
              )}
              {critique.suggestions.length > 0 && (
                <div>
                  <h4 className="text-sm font-medium text-blue-700 dark:text-blue-300 mb-1">Ideas</h4>
                  <ul className="list-disc list-inside space-y-1 text-sm">
                    {critique.suggestions.map((suggestion, idx) => <li key={idx}>{suggestion}</li>)}
                  </ul>
                </div>
              )}
            </div>
          </details>
        )}

        {isEditing && (
          <div className="flex justify-end gap-2 pt-4 border-t border-gray-200 dark:border-gray-700">
            <button onClick={handleCancel} className={btnSecondary} disabled={loading}>
              Cancel
            </button>
            <button data-testid="story-save" onClick={handleSave} className={btnPrimary} disabled={loading}>
              {loading ? 'Saving…' : 'Save changes'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
