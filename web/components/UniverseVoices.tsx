'use client';

import { useState } from 'react';
import { ApiError, postJson } from '@/lib/studio-api';
import { slotToken, type VoicePick, type VoiceSlot } from '@/lib/wizard.mjs';
import type { SavedVoice } from '@/lib/world.mjs';
import { btnPrimary, btnSecondary } from '@/lib/ui';
import { ConfirmButton } from './ConfirmButton';
import { VoicePreview, VoiceSlotPicker, emptyVoice, type VoiceState } from './VoiceSlotPicker';

const UNREACHABLE = 'The studio could not be reached. Check your connection and try again.';

function problemText(e: unknown): string {
  return e instanceof ApiError ? e.message : UNREACHABLE;
}

/**
 * The saved voice of a Universe character (speaker) or of its narrator: find one in plain words with
 * the wizard's voice search (POST /api/voices/pick), play it, then save it on the Universe
 * (`saveUrl`; nothing is generated or charged) or clear it. New episodes in the Universe start with it.
 */
export function SavedVoiceBlock({ slotKey, label, speaker, saved, saveUrl, onSaved, suggestion }: {
  slotKey: string;
  label: string;
  /** The character's name; null for the narrator. */
  speaker: string | null;
  saved: SavedVoice | null;
  saveUrl: string;
  onSaved: () => Promise<void> | void;
  suggestion: string;
}) {
  const [state, setState] = useState<VoiceState>(emptyVoice());
  const [picking, setPicking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ tone: 'ok' | 'problem'; text: string } | null>(null);
  const token = slotToken(slotKey);
  const slot: VoiceSlot = {
    key: slotKey, role: speaker === null ? 'narrator' : 'speaker', label, speaker, lines: 0, suggestion,
  };
  const patch = (next: Partial<VoiceState>) => setState((previous) => ({ ...previous, ...next }));

  const find = async (another: boolean) => {
    const description = state.description.trim();
    if (description.length < 3) {
      patch({ error: 'Describe the voice in a few words first.' });
      return;
    }
    const exclude = another ? state.shown : [];
    patch({ busy: true, error: null });
    try {
      const pick = await postJson<VoicePick>('/api/voices/pick', {
        description, exclude_voice_ids: exclude, role: speaker === null ? 'narrator' : speaker,
      });
      patch({ busy: false, choice: pick, shown: [...exclude, pick.voice_id] });
    } catch (e) {
      patch({ busy: false, error: problemText(e) });
    }
  };

  const save = async (voice: VoicePick | null) => {
    setBusy(true);
    setNote(null);
    try {
      await postJson(saveUrl, voice
        ? { voice_id: voice.voice_id, name: voice.name, description: state.description.trim(), preview_url: voice.preview_url ?? null }
        : { voice_id: null });
      setNote({ tone: 'ok', text: voice ? `Saved. New episodes start with ${voice.name}.` : 'Saved voice cleared.' });
      setPicking(false);
      setState(emptyVoice());
      await onSaved();
    } catch (e) {
      setNote({ tone: 'problem', text: problemText(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-4 border-t border-gray-200 pt-3" data-testid={`saved-voice-${token}`}>
      <h4 className="text-sm font-semibold">{speaker === null ? 'Narrator voice' : 'Voice'}</h4>
      {saved && !picking ? (
        <div className="mt-2 flex flex-wrap items-center gap-3 rounded-lg bg-gray-50 p-3">
          <span className="text-sm">
            Saved voice: <strong data-testid={`saved-voice-name-${token}`}>{saved.name}</strong>
          </span>
          <VoicePreview key={saved.voice_id} pick={{ voice_id: saved.voice_id, name: saved.name, preview_url: saved.preview_url }}
            token={`saved-${token}`} />
          <button type="button" className={btnSecondary} disabled={busy} onClick={() => setPicking(true)}
            data-testid={`saved-voice-change-${token}`}>
            Change
          </button>
          <ConfirmButton question={`Clear the saved voice of ${label}?`} confirmLabel="Clear it" disabled={busy}
            className={btnSecondary} onConfirm={() => save(null)} testId={`saved-voice-clear-${token}`}>
            Clear
          </ConfirmButton>
          {saved.description && <p className="w-full text-xs text-gray-600">Asked for: {saved.description}</p>}
        </div>
      ) : (
        <div className="mt-2">
          <VoiceSlotPicker slot={slot} state={state} onDescription={(text) => patch({ description: text })}
            onFind={() => find(false)} onRetry={() => find(true)}
            note={speaker === null
              ? 'Reads the narration of every new episode in this Universe.'
              : `Speaks ${label}'s lines in every new episode in this Universe.`} />
          <div className="mt-2 flex flex-wrap gap-2">
            {state.choice && (
              <button type="button" className={btnPrimary} disabled={busy || state.busy} onClick={() => save(state.choice)}
                data-testid={`saved-voice-save-${token}`}>
                {busy ? 'Saving…' : 'Save this voice'}
              </button>
            )}
            {saved && (
              <button type="button" className={btnSecondary} disabled={busy}
                onClick={() => { setPicking(false); setState(emptyVoice()); }}>
                Keep {saved.name}
              </button>
            )}
          </div>
        </div>
      )}
      {note && (
        <p className={`mt-2 text-sm ${note.tone === 'ok' ? 'text-green-800' : 'text-red-700'}`}
          role={note.tone === 'problem' ? 'alert' : 'status'} data-testid={`saved-voice-message-${token}`}>
          {note.text}
        </p>
      )}
    </div>
  );
}

export default SavedVoiceBlock;
