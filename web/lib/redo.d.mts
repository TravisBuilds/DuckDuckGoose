/** POST /api/episodes/{id}/shots/{shot_id}/retry/estimate */
export interface RedoEstimate {
  shot_id: string;
  stills: number;
  kinds: string[];
  per_still_usd_micros: number[];
  total_usd_micros: number;
  dry_run: boolean;
  summary: string;
}

export interface RedoEstimateBody { prompt_append?: string }
export interface RedoBody extends RedoEstimateBody { approved_usd_micros: number }

export declare const REDO_PROMPT_MAX: number;
export declare const REDO_PREFIX: string;
export declare const REDO_NOTE_MAX: number;
export declare function redoNote(typed: string | null | undefined, recorded: string | null | undefined): string;
export declare function redoPromptAppend(note: string | null | undefined): string | null;
export declare function redoEstimateRequest(note: string | null | undefined): RedoEstimateBody;
export declare function redoRequest(note: string | null | undefined, estimate: Pick<RedoEstimate, 'total_usd_micros'>): RedoBody;
export declare function redoPriceText(estimate: Pick<RedoEstimate, 'stills' | 'dry_run' | 'total_usd_micros'>): string;
export declare function redoNoteText(note: string | null | undefined): string;
export declare function redoStartedText(shotId: string, note: string | null | undefined): string;
