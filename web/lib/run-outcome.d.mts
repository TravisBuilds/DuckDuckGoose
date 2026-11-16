export interface RunInfo {
  run_id: string;
  status: string;
  ended: boolean;
  outcome: string | null;
  reason: string | null;
  dry_run: boolean | null;
  can_start_again: boolean;
  /** The workflow server purged the run: the API answered from the saved state. */
  archived?: boolean;
  /** When that saved state was written (UTC, "YYYY-MM-DD HH:MM:SS"); null when rebuilt from the saved files. */
  saved_at?: string | null;
}
export declare function runEndedView(run: RunInfo | null | undefined): null | {
  tone: 'error' | 'done' | 'expired';
  title: string;
  reason: string | null;
  canStartAgain: boolean;
  confirm: string | null;
};
export declare function keepPolling(state: { run?: { ended?: boolean | null } | null } | null | undefined): boolean;
