export type StepKey = 'story' | 'cast' | 'shot_plan' | 'stills' | 'final_cut' | 'download';
export type NextStepKind = 'story' | 'cast' | 'shot_plan' | 'stills' | 'final_cut' | 'done' | 'expired' | 'failed' | 'working';
export type StepStatus = 'done' | 'current' | 'todo' | 'skipped' | 'stopped';

export interface RunStateLike {
  stage?: string | null;
  current_gate?: string | null;
  passed_gates?: string[];
  story_shape?: unknown;
  approvals?: Record<string, boolean | undefined>;
  shots_count?: number;
  shot_workflow_ids?: Record<string, unknown>;
  shot_outcomes?: Record<string, unknown>;
  shot_decisions?: Record<string, string>;
  failed_shots?: (string | { shot_id?: string })[];
  run?: {
    ended?: boolean | null;
    outcome?: string | null;
    reason?: string | null;
    dry_run?: boolean | null;
    can_start_again?: boolean | null;
    archived?: boolean | null;
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

export interface ShotStateLike {
  shot_id: string;
  still_status?: string | null;
  pre_approved?: boolean;
}

export interface NextStep {
  step: NextStepKind;
  title: string;
  text: string;
  href: string | null;
  count?: number;
  advisory?: boolean;
  also?: { text: string; href: string };
  waiting?: boolean;
  waitingOn?: string;
}

/** Who reads the Next step (lib/next-step.mjs): the person who made the video when left out. */
export interface Viewer {
  mine?: boolean;
  canAct?: boolean;
  ownerName?: string | null;
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
export function openGate(state: RunStateLike | null | undefined): string | null;
export function storyDone(state: RunStateLike | null | undefined): boolean;
export function storyLocked(state: RunStateLike | null | undefined): boolean;
export function shotIds(state: RunStateLike | null | undefined): string[];
export function stillsToReview(
  state: RunStateLike | null | undefined,
  shots?: ShotStateLike[] | null,
): { review: number; making: number; total: number };
export function charactersToLock(cast: CastLike | null | undefined): string[];
export function listNames(names: string[]): string;
export function finishLine(run: RunStateLike['run'], files: FilesLike, mine?: boolean): FinishLine;
export function nextStep(
  state: RunStateLike | null | undefined,
  story?: StoryLike | null,
  cast?: CastLike | null,
  more?: { files?: FilesLike; shots?: ShotStateLike[] | null; viewer?: Viewer | null } | null,
): NextStep;
export function stepperSteps(
  state: RunStateLike | null | undefined,
  story: StoryLike | null | undefined,
  cast: CastLike | null | undefined,
  next: NextStep | null | undefined,
): StepperStep[] | null;
export type ChipTone = 'ok' | 'todo' | 'wait' | 'warn' | 'bad';
export function statusChip(next: NextStep | null | undefined): { label: string; tone: ChipTone };
export function cardStatus(
  state: RunStateLike | null | undefined,
  story?: StoryLike | null,
  viewer?: Viewer | null,
): { label: string; tone: ChipTone };
