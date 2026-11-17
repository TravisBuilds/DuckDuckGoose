'use client';

import type { NextStep, StepperStep } from '@/lib/next-step.mjs';
import { formatUsd } from '@/lib/money';
import { btnGo, btnSecondary, link, tones } from '@/lib/ui';
import { GooseArt, Icon } from './playful';

const STATE: Record<StepperStep['status'], { cls: string; words: string }> = {
  done: { cls: 'is-done', words: 'done' },
  current: { cls: 'is-now', words: 'now' },
  todo: { cls: 'is-todo', words: 'to do' },
  skipped: { cls: 'is-todo', words: 'skipped' },
  stopped: { cls: 'is-blocked', words: 'stopped here' },
};

/** The steps that spend money in a live run: character sheets, scene pictures, then the clips and voices. */
export const PAID_STEPS = new Set(['cast', 'stills', 'final_cut']);

function stepNote(step: StepperStep, cost: number | null | undefined, live: boolean): string {
  if (step.status === 'done') return cost ? formatUsd(cost) : PAID_STEPS.has(step.key) ? 'Free' : 'Done';
  if (step.status === 'skipped') return 'Skipped';
  if (step.status === 'stopped') return 'Stopped here';
  if (step.status === 'current') return 'Now';
  return PAID_STEPS.has(step.key) ? (live ? 'Priced first' : 'Free test run') : '';
}

/**
 * Story, Cast, Shot plan, Scene pictures, Final cut, Download as the board-game trail: a "$" coin on
 * each step that spends in a live run, and under each done step what it really cost (`costs`, from
 * the episode's budget lines; "Free" in a test run).
 */
export function Stepper({ steps, costs = {}, live = false, testId = 'stepper' }: {
  steps: StepperStep[]; costs?: Record<string, number | null>; live?: boolean; testId?: string;
}) {
  return (
    <div className="overflow-x-auto pb-1">
      <ol className="pf-path min-w-max" aria-label="Steps" data-testid={testId}>
        {steps.map((step, i) => {
          const state = STATE[step.status];
          const paid = PAID_STEPS.has(step.key);
          const note = stepNote(step, costs[step.key], live);
          return (
            <li key={step.key} className={`flex items-start ${i === 0 ? 'flex-none' : 'flex-1'}`}
              data-testid={testId === 'stepper' ? `step-${step.key}` : `${testId}-${step.key}`}
              data-status={step.status} aria-current={step.status === 'current' ? 'step' : undefined}>
              {i > 0 && <span aria-hidden="true" className={`pf-link ${steps[i - 1].status === 'done' ? 'is-done' : ''}`} />}
              <span className={`pf-stop ${state.cls}`}>
                <span className="pf-bub" aria-hidden="true">
                  {step.status === 'done' ? <Icon name="check" size={18} /> : step.status === 'stopped' ? '!' : step.status === 'skipped' ? '–' : i + 1}
                  {paid && <span className="pf-coin">$</span>}
                </span>
                <span className="pf-stop-name">{step.label}</span>
                {note && <span className="pf-stop-note">{note}</span>}
                <span className="sr-only"> ({state.words}{paid ? ', spends money in a live run' : ''})</span>
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/**
 * The episode's gate panel (lib/next-step.mjs): what to do next, with a link to the card that does it,
 * whether the run is a free test or live, and what the episode has really cost so far. Someone who
 * may not change this video reads who the step waits on instead (nextStep's viewer). The approvals
 * themselves stay in their cards, next to the shots or pictures they approve.
 */
export default function NextStepCard({ next, steps, canAct, live = false, spent = null }: {
  next: NextStep;
  steps: StepperStep[] | null;
  canAct: boolean;
  live?: boolean;
  spent?: number | null;
}) {
  const toDo = canAct && !next.waiting && ['story', 'cast', 'shot_plan', 'stills', 'final_cut'].includes(next.step);
  const chip = next.waiting ? { text: `Waiting on ${next.waitingOn}`, tone: tones.wait }
    : toDo ? { text: next.advisory ? 'Next step (optional)' : 'Your OK needed', tone: tones.todo }
      : next.step === 'done' ? { text: 'Finished', tone: tones.ok }
        : next.step === 'failed' ? { text: 'Stopped', tone: tones.bad }
          : { text: 'Next step', tone: tones.run };
  const coming = (steps ?? []).filter((s) => s.status === 'todo').map((s) => s.label);
  return (
    <section data-testid="next-step" data-step={next.step} aria-labelledby="next-step-title">
      <div className="flex items-center gap-3">
        <GooseArt className="h-[64px] w-[58px] shrink-0" />
        <div className="min-w-0">
          <span className={`pf-chip ${chip.tone}`}>{chip.text}</span>
          <h2 id="next-step-title" className="mt-1 text-[20px] leading-tight" data-testid="next-step-title">{next.title}</h2>
        </div>
      </div>
      <p className="mt-2 text-[14px]" data-testid="next-step-text">{next.text}</p>
      <dl className="mt-3 space-y-1.5 rounded-inner border-2 border-[#E5DED0] bg-white p-3 text-[13px]">
        <div className="flex justify-between gap-3">
          <dt>This run</dt>
          <dd className="font-extrabold">{live ? 'Live: paid providers' : 'Test run: free'}</dd>
        </div>
        {spent !== null && (
          <div className="flex justify-between gap-3">
            <dt>Spent on this episode so far</dt>
            <dd className="font-extrabold" data-testid="next-step-spent">{formatUsd(spent)}</dd>
          </div>
        )}
      </dl>
      {(next.href || next.also) && (
        <div className="mt-3 flex flex-wrap items-center gap-3">
          {next.href && (
            <a href={next.href} className={toDo ? `${btnGo} w-full` : `${btnSecondary} pf-btn-sm`} data-testid="next-step-link">
              {toDo ? 'Show me' : 'Go there'}
            </a>
          )}
          {next.also && (
            <a href={next.also.href} className={`${link} text-[13px] font-extrabold`}>{next.also.text}</a>
          )}
        </div>
      )}
      {toDo && (
        <p className="mt-2 text-center text-[12px] text-ink-soft">
          Every paid step shows its price first, and the studio checks it and your balance again before it spends.
        </p>
      )}
      {coming.length > 0 && next.step !== 'done' && (
        <p className="mt-3 rounded-inner bg-cream p-3 text-[12.5px]">
          <span className="font-extrabold">Coming up:</span> {coming.join(', then ')}.
        </p>
      )}
    </section>
  );
}
