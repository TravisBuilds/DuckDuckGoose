'use client';

import { PAID_STEPS, STEP_LABELS, runLine, stepNote, trailWord, type NextStep, type StepperStep } from '@/lib/next-step.mjs';
import { priceText } from '@/lib/checkout.mjs';
import { btnPrimary, btnSecondary, link, tones } from '@/lib/ui';

const STATE: Record<StepperStep['status'], { cls: string; words: string }> = {
  done: { cls: 'is-done', words: 'done' },
  current: { cls: 'is-now', words: 'now' },
  todo: { cls: 'is-todo', words: 'to do' },
  skipped: { cls: 'is-todo', words: 'skipped' },
  stopped: { cls: 'is-blocked', words: 'stopped here' },
};

/**
 * Story, Cast, Shot plan, Scene pictures, Final cut, Download as the board-game trail: each step says
 * only where it is (done, now). What each one cost is in the cost breakdown, not on the trail.
 */
export function Stepper({ steps, testId = 'stepper' }: { steps: StepperStep[]; testId?: string }) {
  return (
    <div className="overflow-x-auto pb-1">
      <ol className="pf-path min-w-max" aria-label="Steps" data-testid={testId}>
        {steps.map((step, i) => {
          const state = STATE[step.status];
          const note = trailWord(step);
          return (
            <li key={step.key} className={`flex items-start ${i === 0 ? 'flex-none' : 'flex-1'}`}
              data-testid={testId === 'stepper' ? `step-${step.key}` : `${testId}-${step.key}`}
              data-status={step.status} aria-current={step.status === 'current' ? 'step' : undefined}>
              {i > 0 && <span aria-hidden="true" className={`pf-link ${steps[i - 1].status === 'done' ? 'is-done' : ''}`} />}
              <span className={`pf-stop ${state.cls}`}>
                <span className="pf-bub" aria-hidden="true">{i + 1}</span>
                <span className="pf-stop-name">{step.label}</span>
                {note && <span className="pf-stop-note">{note}</span>}
                <span className="sr-only"> ({state.words})</span>
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/**
 * What each paid step cost, behind "See the breakdown" (lib/next-step.mjs stepNote): its recorded
 * spend (`costs`, from the episode's budget lines, null while the budget is unread), "Free" only for a
 * recorded $0 in a test run, `dryRun` being the run's own record.
 */
export function CostBreakdown({ steps, costs, live = false, dryRun = null }: {
  steps: StepperStep[] | null; costs: Record<string, number | null> | null; live?: boolean; dryRun?: boolean | null;
}) {
  return (
    <details className="mt-3 text-[13px]" data-testid="cost-breakdown">
      <summary className="cursor-pointer font-extrabold text-blue-deep">See the breakdown</summary>
      <dl className="mt-2 space-y-1.5 rounded-inner bg-page p-3">
        {PAID_STEPS.map((key) => {
          const step = steps?.find((s) => s.key === key) ?? { key, status: 'todo' as const };
          return (
            <div key={key} className="flex justify-between gap-3">
              <dt>{STEP_LABELS[key]}</dt>
              <dd className="font-extrabold" data-testid={`cost-${key}`}>
                {stepNote(step, { cost: costs === null ? undefined : costs[key] ?? null, live, dryRun })}
              </dd>
            </div>
          );
        })}
      </dl>
    </details>
  );
}

/**
 * The episode's gate panel (lib/next-step.mjs): what to do next, with a link to the card that does it,
 * and one quiet money line (a test run is free; a live run shows what it has spent so far, with the
 * breakdown behind a disclosure). Someone who may not change this video reads who the step waits on
 * instead (nextStep's viewer). The approvals themselves stay in their cards, next to the shots or
 * pictures they approve, each with its own price.
 */
export default function NextStepCard({ next, steps, canAct, live = false, spent = null, costs = null, dryRun = null }: {
  next: NextStep;
  steps: StepperStep[] | null;
  canAct: boolean;
  live?: boolean;
  spent?: number | null;
  costs?: Record<string, number | null> | null;
  dryRun?: boolean | null;
}) {
  const toDo = canAct && !next.waiting && ['story', 'cast', 'shot_plan', 'stills', 'final_cut'].includes(next.step);
  const chip = next.waiting ? { text: `Waiting on ${next.waitingOn}`, tone: tones.wait }
    : toDo ? { text: next.advisory ? 'Next step (optional)' : 'Your turn', tone: tones.todo }
      : next.step === 'done' ? { text: 'Finished', tone: tones.ok }
        : next.step === 'failed' ? { text: 'Stopped', tone: tones.bad }
          : { text: 'Next step', tone: tones.run };
  const coming = (steps ?? []).filter((s) => s.status === 'todo').map((s) => s.label);
  return (
    <section data-testid="next-step" data-step={next.step} aria-labelledby="next-step-title">
      <div className="min-w-0">
        <span className={`pf-chip ${chip.tone}`}>{chip.text}</span>
        <h2 id="next-step-title" className="mt-1 text-[20px] leading-tight" data-testid="next-step-title">{next.title}</h2>
      </div>
      <p className="mt-2 text-[14px]" data-testid="next-step-text">{next.text}</p>
      {(next.href || next.also) && (
        <div className="mt-3 flex flex-wrap items-center gap-3">
          {next.href && (
            <a href={next.href} className={toDo ? `${btnPrimary} w-full` : `${btnSecondary} pf-btn-sm`} data-testid="next-step-link">
              {toDo ? 'Show me' : 'Go there'}
            </a>
          )}
          {next.also && (
            <a href={next.also.href} className={`${link} text-[13px] font-extrabold`}>{next.also.text}</a>
          )}
        </div>
      )}
      <p className="mt-3 text-[13px] text-ink-soft" data-testid="next-step-money">
        {runLine({ live })}
        {live && spent !== null && <> Spent so far: <b className="text-ink" data-testid="next-step-spent">{priceText(spent)}</b>.</>}
      </p>
      {live && spent !== null && spent > 0 && <CostBreakdown steps={steps} costs={costs} live={live} dryRun={dryRun} />}
      {coming.length > 0 && next.step !== 'done' && (
        <p className="mt-3 rounded-inner bg-page p-3 text-[12.5px]">
          <span className="font-extrabold">Coming up:</span> {coming.join(', then ')}.
        </p>
      )}
    </section>
  );
}
