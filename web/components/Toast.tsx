'use client';

import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { focusRing } from '@/lib/ui';

export type Notice = { text: string; tone: 'ok' | 'bad'; status?: number };

export const TOAST_HOST_ID = 'studio-toasts';

/** How long a confirmation stays up; problems stay until dismissed or replaced. */
const OK_MS = 8000;

/**
 * The result of an action, pinned to the bottom of the viewport so it is in view wherever the button
 * was. Renders into the studio shell's role="status" region (#studio-toasts), so screen readers announce
 * it; falls back to rendering in place outside the shell.
 */
export function Toast({ notice, onDismiss, testId }: {
  notice: Notice | null;
  onDismiss: () => void;
  testId: string;
}) {
  const [host, setHost] = useState<HTMLElement | null>(null);

  useEffect(() => {
    setHost(document.getElementById(TOAST_HOST_ID));
  }, []);

  useEffect(() => {
    if (notice?.tone !== 'ok') return;
    const timer = window.setTimeout(onDismiss, OK_MS);
    return () => window.clearTimeout(timer);
  }, [notice, onDismiss]);

  if (!notice) return null;
  const tone = notice.tone === 'bad'
    ? 'border-red-300 bg-red-50 text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100'
    : 'border-green-300 bg-green-50 text-green-900 dark:border-green-800 dark:bg-green-950 dark:text-green-100';
  const body = (
    <div className={`pointer-events-auto flex w-full max-w-md items-start gap-3 rounded-lg border px-4 py-3 shadow-lg ${tone}`}>
      <span aria-hidden="true" className="mt-0.5 text-sm font-bold">{notice.tone === 'bad' ? '!' : '✓'}</span>
      <p className="flex-1 text-sm" data-testid={testId} data-tone={notice.tone} data-status={notice.status}>
        {notice.text}
      </p>
      <button type="button" onClick={onDismiss} aria-label="Dismiss" className={`rounded px-1 text-lg leading-none ${focusRing}`}>
        ×
      </button>
    </div>
  );
  return host ? createPortal(body, host) : <div role="status">{body}</div>;
}

/** The pinned, announced region toasts render into (one per page, in the studio shell). */
export function ToastHost() {
  return (
    <div id={TOAST_HOST_ID} role="status" aria-live="polite"
      className="pointer-events-none fixed inset-x-0 bottom-4 z-50 flex flex-col items-center gap-2 px-4" />
  );
}
