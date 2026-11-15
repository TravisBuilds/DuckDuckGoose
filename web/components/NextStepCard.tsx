'use client';

import type { NextStep, StepperStep } from '@/lib/next-step.mjs';
import { btnPrimary, highlight, link } from '@/lib/ui';

const STEP_STYLE: Record<StepperStep['status'], { dot: string; text: string; label: string }> = {
  done: { dot: 'bg-green-600 text-white', text: 'text-gray-900 dark:text-gray-100', label: 'done' },
  current: { dot: 'bg-blue-600 text-white ring-4 ring-blue-100 dark:ring-blue-900', text: 'font-semibold text-blue-800 dark:text-blue-200', label: 'now' },
  todo: { dot: 'border-2 border-gray-300 bg-white text-gray-700 dark:border-gray-500 dark:bg-gray-800 dark:text-gray-200', text: 'text-gray-700 dark:text-gray-200', label: 'to do' },
  skipped: { dot: 'border-2 border-dashed border-gray-300 bg-white text-gray-600 dark:border-gray-500 dark:bg-gray-800 dark:text-gray-300', text: 'text-gray-600 dark:text-gray-300', label: 'skipped' },
  stopped: { dot: 'bg-red-600 text-white', text: 'font-semibold text-red-800 dark:text-red-200', label: 'stopped here' },
};

/** Story, Cast, Shot plan, Scene pictures, Final cut, Download. */
export function Stepper({ steps }: { steps: StepperStep[] }) {
  return (
    <ol className="flex flex-wrap gap-x-4 gap-y-2" aria-label="Steps" data-testid="stepper">
      {steps.map((step, i) => {
        const style = STEP_STYLE[step.status];
        return (
          <li key={step.key} className="flex items-center gap-2 text-sm" data-testid={`step-${step.key}`} data-status={step.status}
            aria-current={step.status === 'current' ? 'step' : undefined}>
            <span aria-hidden="true" className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-bold ${style.dot}`}>
              {step.status === 'done' ? '✓' : step.status === 'stopped' ? '!' : step.status === 'skipped' ? '–' : i + 1}
            </span>
            <span className={style.text}>
              {step.label}
              <span className="sr-only"> ({style.label})</span>
            </span>
          </li>
        );
      })}
    </ol>
  );
}

/**
 * The top of the episode page: what to do next, with a link to the card that does it (lib/next-step.mjs).
 * Someone who may not change this video sees the same step, with a note that it is not theirs to take.
 */
export default function NextStepCard({ next, steps, canAct }: {
  next: NextStep;
  steps: StepperStep[] | null;
  canAct: boolean;
}) {
  const waiting = ['story', 'cast', 'shot_plan', 'stills', 'final_cut'].includes(next.step);
  return (
    <section className={highlight} data-testid="next-step" data-step={next.step}
      aria-labelledby="next-step-title">
      {steps && <Stepper steps={steps} />}
      <p className={`text-xs font-semibold uppercase tracking-wide text-blue-800 dark:text-blue-200 ${steps ? 'mt-4' : ''}`}>
        {next.advisory ? 'Next step (optional)' : 'Next step'}
      </p>
      <h2 id="next-step-title" className="mt-1 text-xl font-bold text-gray-900 dark:text-white" data-testid="next-step-title">
        {next.title}
      </h2>
      <p className="mt-1 text-gray-700 dark:text-gray-200" data-testid="next-step-text">{next.text}</p>
      {waiting && !canAct && (
        <p className="mt-2 text-sm text-gray-700 dark:text-gray-200">
          This is someone else&apos;s video: only the person who made it, or an admin, can take this step.
        </p>
      )}
      {(next.href || next.also) && (
        <div className="mt-4 flex flex-wrap items-center gap-4">
          {next.href && (
            <a href={next.href} className={waiting && canAct ? btnPrimary : `${link} text-sm font-semibold`} data-testid="next-step-link">
              {waiting && canAct ? 'Show me' : 'Go there'}
            </a>
          )}
          {next.also && (
            <a href={next.also.href} className={`${link} text-sm font-semibold`}>{next.also.text}</a>
          )}
        </div>
      )}
    </section>
  );
}
