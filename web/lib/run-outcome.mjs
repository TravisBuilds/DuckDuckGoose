/**
 * The episode page's line for a workflow run that has ended (`run` in GET /api/episodes/{id}).
 *
 * An ended run (failed, terminated, canceled, timed out or completed) takes no more actions: the API
 * refuses them with a 409. The page says how the run ended and why, and offers Start again
 * (POST /api/episodes/{id}/start-again) when the API allows it.
 */

const TITLES = {
  failed: 'This run failed.',
  terminated: 'This run was terminated.',
  canceled: 'This run was canceled.',
  'timed out': 'This run timed out.',
  completed: 'This run completed.',
};

/**
 * @returns {null | { tone: 'error' | 'done', title: string, reason: string | null,
 *   canStartAgain: boolean, confirm: string | null }}
 */
export function runEndedView(run) {
  if (!run || !run.ended) return null;
  const outcome = run.outcome || String(run.status || 'ended').toLowerCase();
  const done = outcome === 'completed';
  const canStartAgain = !done && run.can_start_again === true;
  return {
    tone: done ? 'done' : 'error',
    title: TITLES[outcome] || `This run has ended (${outcome}).`,
    reason: done ? null : run.reason || null,
    canStartAgain,
    confirm: canStartAgain && run.dry_run === false
      ? 'Start a new LIVE run with the same inputs? It can spend credits again.'
      : null,
  };
}
