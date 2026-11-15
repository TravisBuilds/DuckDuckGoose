export type StepKey = 'story' | 'cast' | 'shot_plan' | 'stills' | 'final_cut' | 'download';
export type NextStepKind = 'story' | 'cast' | 'shot_plan' | 'stills' | 'final_cut' | 'done' | 'failed' | 'working';
export type StepStatus = 'done' | 'current' | 'todo' | 'skipped' | 'stopped';

export interface RunStateLike {
  stage?: string | null;
  current_gate?: string | null;
  passed_gates?: string[];
  story_shape?: unknown;
  approvals?: Record<string, boolean | undefined>;
  shot_workflow_ids?: Record<string, unknown>;
  shot_outcomes?: Record<string, unknown>;
  shot_decisions?: Record<string, string>;
  run?: {
    ended?: boolean;
    outcome?: string | null;
    dry_run?: boolean;
    can_start_again?: boolean;
  } | null;
}

export interface StoryLike {
  story_shape?: unknown;
  approved?: boolean;
}

export interface CastLike {
  characters?: {
    slug: string;
    name?: string;
    default_version?: number;
    versions?: { version: number; locked?: boolean }[];
  }[];
  shots?: { shots?: { cast?: (string | { character: string; version?: number | null })[] }[] };
}

export type FilesLike = Record<string, { available?: boolean } | undefined> | null | undefined;

export interface NextStep {
  step: NextStepKind;
  title: string;
  text: string;
  href: string | null;
  count?: number;
  advisory?: boolean;
  also?: { text: string; href: string };
}

export interface FinishLine {
  ready: boolean;
  primary: string | null;
  title: string;
  text: string;
}

export interface StepperStep {
  key: StepKey;
  label: string;
  status: StepStatus;
}

export const STEPS: StepKey[];
export const STEP_LABELS: Record<StepKey, string>;
export const VIDEO_FILES: string[];
export function storyDone(state: RunStateLike | null | undefined): boolean;
export function storyLocked(state: RunStateLike | null | undefined): boolean;
export function shotIds(state: RunStateLike | null | undefined): string[];
export function charactersToLock(cast: CastLike | null | undefined): string[];
export function listNames(names: string[]): string;
export function finishLine(run: RunStateLike['run'], files: FilesLike): FinishLine;
export function nextStep(
  state: RunStateLike | null | undefined,
  story?: StoryLike | null,
  cast?: CastLike | null,
  files?: FilesLike,
): NextStep;
export function stepperSteps(
  state: RunStateLike | null | undefined,
  cast: CastLike | null | undefined,
  next: NextStep | null | undefined,
): StepperStep[];
export function statusChip(next: NextStep | null | undefined): { label: string; tone: 'ok' | 'todo' | 'wait' | 'warn' | 'bad' };
