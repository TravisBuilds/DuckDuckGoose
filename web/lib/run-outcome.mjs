/**
 * The episode page's line for a workflow run that has ended (`run` in GET /api/episodes/{id}).
 *
 * An ended run (failed, terminated, canceled, timed out or completed) takes no more actions: the API
 * refuses them with a 409. The page says how the run ended and why, and offers Start again
 * (POST /api/episodes/{id}/start-again) when the API allows it.
 *
 * Once the workflow server has purged a run's history, the API answers from the saved state instead
 * (run.archived, run.status "ARCHIVED"): outcome "completed" when it shows the approved final cut,
 * "expired" when nothing saved shows how the run ended.
 */

const TITLES = {
  failed: 'This run failed.',
  terminated: 'This run was terminated.',
  canceled: 'This run was canceled.',
  'timed out': 'This run timed out.',
  completed: 'This run completed.',
  expired: "This run's history expired.",
};

/**
 * @returns {null | { tone: 'error' | 'done' | 'expired', title: string, reason: string | null,
 *   canStartAgain: boolean, confirm: string | null }}
 */
export function runEndedView(run) {
  if (!run || !run.ended) return null;
  const outcome = run.outcome || String(run.status || 'ended').toLowerCase();
  const done = outcome === 'completed';
  const expired = outcome === 'expired';
  const canStartAgain = !done && !expired && run.can_start_again === true;
  return {
    tone: done ? 'done' : expired ? 'expired' : 'error',
    title: TITLES[outcome] || `This run has ended (${outcome}).`,
    reason: done ? null : run.reason || null,
    canStartAgain,
    confirm: canStartAgain && run.dry_run === false
      ? 'Start this video again, live? It is paid for again, and each paid step shows its price first.'
      : null,
  };
}

/**
 * Whether the episode page keeps polling the run. An ended run (archived ones included) takes no more
 * actions, so its state stops changing: the page reads it and stops until one of its own actions
 * (Start again) brings a new run. With no run yet (null) it keeps polling, as the video may start.
 */
export function keepPolling(state) {
  return !state?.run?.ended;
}
