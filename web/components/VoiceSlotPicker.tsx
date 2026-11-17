'use client';

import React, { useRef, useState } from 'react';
import { slotToken, type VoicePick, type VoiceSlot } from '@/lib/wizard.mjs';

/** One voice slot while a voice is being found (the wizard's voice step and a Universe's saved voices). */
export interface VoiceState {
  description: string;
  choice: VoicePick | null;
  shown: string[];
  busy: boolean;
  error: string | null;
}

export function emptyVoice(): VoiceState {
  return { description: '', choice: null, shown: [], busy: false, error: null };
}

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function VoicePreview({ pick, token }: { pick: VoicePick; token: string }) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [playError, setPlayError] = useState<string | null>(null);

  if (!pick.preview_url) {
    return <span className="text-xs text-gray-500">No preview for this voice</span>;
  }

  const toggle = async () => {
    const audio = audioRef.current;
    if (!audio) return;
    if (!audio.paused) {
      audio.pause();
      audio.currentTime = 0;
      return;
    }
    setPlayError(null);
    try {
      await audio.play();
    } catch (e) {
      setPlayError(`Could not play the preview: ${errorText(e)}`);
    }
  };

  return (
    <>
      <button
        type="button"
        onClick={toggle}
        className="pf-btn pf-btn-quiet pf-btn-sm"
        data-testid={`voice-play-${token}`}
      >
        {playing ? '■ Stop' : '▶ Play preview'}
      </button>
      <audio
        ref={audioRef}
        src={pick.preview_url}
        preload="none"
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        data-testid={`voice-audio-${token}`}
      />
      {playError && <span className="text-xs font-bold text-coral-ink">{playError}</span>}
    </>
  );
}

/** note: the line under the label (default: how many voice-over lines the slot has). */
export function VoiceSlotPicker({
  slot,
  state,
  onDescription,
  onFind,
  onRetry,
  note,
}: {
  slot: VoiceSlot;
  state: VoiceState;
  onDescription: (text: string) => void;
  onFind: () => void;
  onRetry: () => void;
  note?: string;
}) {
  const token = slotToken(slot.key);
  const inputId = `voice-description-${token}`;
  const choice = state.choice;
  return (
    <div className="border-t border-gray-200 pt-4 first:border-t-0 first:pt-0" data-testid={`voice-slot-${token}`}>
      <label htmlFor={inputId} className="block text-sm font-semibold">
        {slot.label}: describe the voice
      </label>
      <p className="text-xs text-gray-500 mb-2">
        {note ?? `${slot.lines} voice-over line${slot.lines === 1 ? '' : 's'}`}
      </p>
      <div className="flex flex-wrap gap-2">
        <input
          id={inputId}
          type="text"
          value={state.description}
          onChange={(e) => onDescription(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !state.busy) onFind();
          }}
          maxLength={300}
          placeholder={`e.g. ${slot.suggestion}`}
          className="pf-input min-w-[10rem] flex-1 text-sm"
          data-testid={inputId}
        />
        <button
          type="button"
          onClick={onFind}
          disabled={state.busy || state.description.trim().length < 3}
          className="pf-btn pf-btn-primary pf-btn-sm shrink-0"
          data-testid={`voice-find-${token}`}
        >
          {state.busy ? 'Finding…' : 'Find a voice'}
        </button>
      </div>
      {state.error && (
        <p className="mt-2 text-sm font-bold text-coral-ink" data-testid={`voice-error-${token}`}>
          {state.error}
        </p>
      )}
      {choice && (
        <div
          className="mt-3 flex flex-wrap items-center gap-3 rounded-inner bg-page p-3"
          data-testid={`voice-choice-${token}`}
          data-voice-id={choice.voice_id}
        >
          <span className="text-sm">
            Voice: <strong data-testid={`voice-name-${token}`}>{choice.name}</strong>
          </span>
          <VoicePreview key={choice.voice_id} pick={choice} token={token} />
          <button
            type="button"
            onClick={onRetry}
            disabled={state.busy}
            className="pf-btn pf-btn-quiet pf-btn-sm"
            data-testid={`voice-retry-${token}`}
          >
            Try another voice
          </button>
          {choice.reason && <p className="w-full text-xs text-gray-500">{choice.reason}</p>}
        </div>
      )}
    </div>
  );
}

export default VoiceSlotPicker;
