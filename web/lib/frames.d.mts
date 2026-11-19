export interface FrameOption {
  option_id: string;
  position: number;
  status: 'queued' | 'running' | 'completed' | 'failed' | 'refused' | 'pending_reconcile' | 'skipped';
  placeholder: boolean;
  width: number | null;
  height: number | null;
  error: string | null;
  image_url: string | null;
  thumb_url: string | null;
}

export interface FrameRound {
  set_id: string;
  shot_id: string;
  round: number;
  status: 'making' | 'ready' | 'partial' | 'failed';
  changes: string[];
  option_count: number;
  dry_run: boolean;
  estimate_usd_micros: number;
  error: string | null;
  requested_by: string | null;
  created_at: string;
  finished_at: string | null;
  made: number;
  current: boolean;
  options: FrameOption[];
}

export interface FramePick {
  source: 'here' | 'shot_list';
  option_id: string | null;
  image_url: string | null;
  thumb_url: string | null;
  stale: boolean;
  picked_by: string | null;
  picked_at: string | null;
}

export interface FrameShot {
  shot_id: string;
  /** The first frame in a few plain words ("Mara at Harbour cabin, dusk, close on her face"); never the prompt. */
  description: string;
  /** The story moment (null for a reader who may not read the shot's words). */
  beat: string | null;
  time_of_day: string | null;
  characters: string[];
  place: string | null;
  props: string[];
  references: number;
  /** The characters, places and props in it with no reference photo yet. */
  no_photos: string[];
  aspect_ratio: string;
  notes: string[];
  problems: string[];
  ready: boolean;
  changes: string[];
  making: boolean;
  changed: boolean;
  rounds: FrameRound[];
  pick: FramePick | null;
}

export interface FramesView {
  episode_id: string;
  options_per_round: number;
  dry_run: boolean;
  words_hidden: boolean;
  uploaded: boolean;
  shots: FrameShot[];
  error: string | null;
  may_change: boolean;
  phase: 'open' | 'g1' | 'locked';
  locked_reason: string | null;
  can_change: boolean;
}

export interface FrameEstimate {
  shot_id: string;
  round: number;
  count: number;
  per_image_usd_micros: number[];
  total_usd_micros: number;
  dry_run: boolean;
  changes: string[];
  characters: string[];
  place: string | null;
  props: string[];
  references: number;
  notes: string[];
  budget: { cap_usd_micros: number; stop_usd_micros: number; spent_usd_micros: number; reserved_usd_micros: number;
    available_usd_micros: number } | null;
  summary: string;
}

export type FrameTone = 'ok' | 'todo' | 'run' | 'bad' | 'wait';

export declare const CHANGE_EXAMPLES: string[];
export declare const CHANGE_MAX: number;
export declare function listWords(words: string[] | null | undefined): string;
export declare function inItLine(shot: Pick<FrameShot, 'characters' | 'place' | 'props'> | null | undefined): string;
export declare function aboutLine(shot: Pick<FrameShot, 'description' | 'characters' | 'place' | 'props'> | null | undefined): string;
export declare function referencesLine(count: number, missing?: string[] | null): string;
export declare function latestRound(shot: FrameShot | null | undefined): FrameRound | null;
export declare function shotStatus(shot: FrameShot | null | undefined): { tone: FrameTone; text: string };
export declare function framesSummary(view: Pick<FramesView, 'shots'> | null | undefined): string;
export declare function framesCount(view: Pick<FramesView, 'shots'> | null | undefined): { picked: number; shots: number };
export declare function priceTotal(estimate: FrameEstimate | null | undefined): string;
export declare function priceStatus(estimate: FrameEstimate | null | undefined): string;
export declare function priceRows(estimate: FrameEstimate | null | undefined): { label: string; value: string }[];
export declare function makeLabel(estimate: FrameEstimate | null | undefined): string;
export declare function changesLine(changes: string[] | null | undefined): string | null;
export declare function changesParts(changes: string[] | null | undefined): { label: string; text: string } | null;
export declare function roundTitle(round: FrameRound | null | undefined): string;
export declare function roundNote(round: FrameRound | null | undefined): string | null;
export declare function changeBlocker(text: string | null | undefined): string | null;
export declare function shotBlocker(view: FramesView | null | undefined, shot: FrameShot | null | undefined): string | null;
export declare function optionName(option: Pick<FrameOption, 'position'> | null | undefined): string;
export declare function pickedOption(shot: FrameShot | null | undefined): string | null;
export declare function anyMaking(view: Pick<FramesView, 'shots'> | null | undefined): boolean;
