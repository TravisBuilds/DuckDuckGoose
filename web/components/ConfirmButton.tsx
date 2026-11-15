'use client';

import { useState, type ReactNode } from 'react';
import { btnDanger, btnSecondary } from '@/lib/ui';

/**
 * A button that asks "are you sure?" in place instead of a browser dialog: the first press shows the
 * question with Yes and Cancel next to it. With no question it acts on the first press.
 */
export function ConfirmButton({
  question, onConfirm, children, className, disabled, testId, confirmLabel = 'Yes', cancelLabel = 'Cancel',
}: {
  question: string | null;
  onConfirm: () => void;
  children: ReactNode;
  className: string;
  disabled?: boolean;
  testId?: string;
  confirmLabel?: string;
  cancelLabel?: string;
}) {
  const [asking, setAsking] = useState(false);

  if (asking && question) {
    return (
      <span role="group" aria-label={question} className="inline-flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">{question}</span>
        <button type="button" className={btnDanger} disabled={disabled}
          data-testid={testId ? `${testId}-confirm` : undefined}
          onClick={() => { setAsking(false); onConfirm(); }}>
          {confirmLabel}
        </button>
        <button type="button" className={btnSecondary} onClick={() => setAsking(false)}>{cancelLabel}</button>
      </span>
    );
  }
  return (
    <button type="button" className={className} disabled={disabled} data-testid={testId}
      onClick={() => (question ? setAsking(true) : onConfirm())}>
      {children}
    </button>
  );
}
