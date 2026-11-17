import type { UniverseCard, WorldBoard, WorldView } from './world.mjs';

export type CreationStep = 'spark' | 'references' | 'moodboard' | 'bible' | 'lock';
export type StepState = 'done' | 'now' | 'todo';

export declare const CREATION_STEPS: CreationStep[];
export declare const CREATION_NAMES: Record<CreationStep, string>;
export declare const BOARD_MIN_PHOTOS: number;

export declare function boardUnlocked(notes: number | null | undefined, photos: number | null | undefined): boolean;
export declare function boardUnlockHint(notes: number | null | undefined, photos: number | null | undefined): string | null;

export interface CreationJourney {
  steps: { key: CreationStep; name: string; note: string; state: StepState }[];
  current: CreationStep | null;
  counts: { notes: number; photos: number; boards: number; characters: number; locked: number; things: number };
  latest: WorldBoard | null;
  approved: WorldBoard | null;
  bibleDone: boolean;
  boardOpen: boolean;
}

export declare function creationJourney(view: WorldView | null | undefined): CreationJourney;
export declare function stepFromQuery(raw: string | null | undefined, journey: { current: CreationStep | null } | null): CreationStep;
export declare function stepNumber(key: CreationStep): number;

export interface HomeJourney {
  steps: { key: CreationStep; name: string; state: StepState }[] | null;
  current: CreationStep | null;
  step: number;
  next: { text: string; tone: 'needs' | 'working' | 'ok' | 'go'; step: CreationStep | null };
}

export declare function homeJourney(card: UniverseCard | null | undefined): HomeJourney;

export interface SealWords {
  state: 'locked' | 'open' | 'drift';
  top: string;
  bottom: string;
}

export declare function sealFor(
  counts: { characters?: number; locked_characters?: number } | null | undefined,
  drift?: number,
): SealWords;
export declare function toneChips(text: string | null | undefined, max?: number): string[];
