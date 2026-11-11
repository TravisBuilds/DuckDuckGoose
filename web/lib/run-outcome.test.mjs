import test from 'node:test';
import assert from 'node:assert/strict';

import { runEndedView } from './run-outcome.mjs';

const failed = {
  run_id: 'r1', status: 'FAILED', ended: true, outcome: 'failed',
  reason: 'revise_story_shape_activity: TypeError: Object of type datetime is not JSON serializable',
  dry_run: true, can_start_again: true,
};

test('a running run (or none reported) shows nothing', () => {
  assert.equal(runEndedView(null), null);
  assert.equal(runEndedView(undefined), null);
  assert.equal(runEndedView({ ...failed, status: 'RUNNING', ended: false, outcome: null, reason: null }), null);
});

test('a failed run says so with its reason and offers Start again', () => {
  assert.deepEqual(runEndedView(failed), {
    tone: 'error',
    title: 'This run failed.',
    reason: 'revise_story_shape_activity: TypeError: Object of type datetime is not JSON serializable',
    canStartAgain: true,
    confirm: null,
  });
});

test('starting a live run again asks first', () => {
  assert.match(runEndedView({ ...failed, dry_run: false }).confirm, /LIVE run/);
});

test('terminated, canceled and timed out runs', () => {
  assert.equal(runEndedView({ ...failed, status: 'TERMINATED', outcome: 'terminated', reason: 'stopped by ops' }).title,
    'This run was terminated.');
  assert.equal(runEndedView({ ...failed, status: 'CANCELED', outcome: 'canceled' }).title, 'This run was canceled.');
  assert.equal(runEndedView({ ...failed, status: 'TIMED_OUT', outcome: 'timed out' }).title, 'This run timed out.');
});

test('a run the API will not start again (e.g. a reopened run) gets no Start again', () => {
  const view = runEndedView({ ...failed, can_start_again: false });
  assert.equal(view.canStartAgain, false);
  assert.equal(view.confirm, null);
});

test('a completed run is done: no reason, no Start again', () => {
  assert.deepEqual(runEndedView({ ...failed, status: 'COMPLETED', outcome: 'completed', reason: null, can_start_again: false }), {
    tone: 'done', title: 'This run completed.', reason: null, canStartAgain: false, confirm: null,
  });
});

test('an outcome the page does not know still reads as ended', () => {
  assert.equal(runEndedView({ ...failed, outcome: null, status: 'SOMETHING_NEW' }).title, 'This run has ended (something_new).');
});
