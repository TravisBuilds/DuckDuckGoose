export interface RunInfo {
  run_id: string;
  status: string;
  ended: boolean;
  outcome: string | null;
  reason: string | null;
  dry_run: boolean | null;
  can_start_again: boolean;
}
export declare function runEndedView(run: RunInfo | null | undefined): null | {
  tone: 'error' | 'done';
  title: string;
  reason: string | null;
  canStartAgain: boolean;
  confirm: string | null;
};
