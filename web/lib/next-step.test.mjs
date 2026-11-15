import test from 'node:test';
import assert from 'node:assert/strict';

import {
  charactersToLock, finishLine, listNames, nextStep, shotIds, statusChip, stepperSteps, storyDone, storyLocked,
} from './next-step.mjs';

const OPEN_RUN = { ended: false, outcome: null, dry_run: true, can_start_again: false };

function runState(overrides = {}) {
  return {
    stage: 'script',
    current_gate: null,
    passed_gates: [],
    story_shape: null,
    approvals: { story_shape: false, g1_shot_plan: false, g2_scene_stills: false, g_final_cut: false },
    shot_workflow_ids: {},
    shot_outcomes: {},
    shot_decisions: {},
    run: OPEN_RUN,
    ...overrides,
  };
}

const STORY = { title: 'The lost kite', beats: [{ beat_id: 'b1' }] };

function castView({ locked = {}, picks = {} } = {}) {
  const character = (slug, name) => ({
    slug,
    name,
    default_version: 1,
    versions: [
      { version: 1, locked: Boolean(locked[`${slug}:1`]) },
      { version: 2, locked: Boolean(locked[`${slug}:2`]) },
    ],
  });
  return {
    characters: [character('mara', 'Mara'), character('theo', 'Theo')],
    shots: { shots: Object.entries(picks).map(([shot_id, cast]) => ({ shot_id, cast })) },
  };
}

test('no run yet: nothing to do, plain words', () => {
  const next = nextStep(null);
  assert.equal(next.step, 'working');
  assert.equal(next.href, null);
  assert.match(next.title, /hasn't started/);
});

test('a story outline waiting for approval is the next step', () => {
  const next = nextStep(runState({ story_shape: STORY }), { story_shape: STORY, approved: false });
  assert.deepEqual([next.step, next.href, next.title], ['story', '#story', 'Read and approve your story']);
});

test('the story outline from GET /story-shape counts when the run state has none yet', () => {
  assert.equal(nextStep(runState(), { story_shape: STORY, approved: false }).step, 'story');
  // a backend that reports approval ends the story step
  assert.equal(nextStep(runState(), { story_shape: STORY, approved: true }).step, 'working');
});

test('a story still being written is work in progress, not a step', () => {
  const next = nextStep(runState({ stage: 'info_gathering' }));
  assert.equal(next.step, 'working');
  assert.equal(next.href, null);
});

test('the story is done once approved or once the run has moved past it', () => {
  assert.equal(storyDone(runState({ story_shape: STORY })), false);
  assert.equal(storyDone(runState({ story_shape: STORY, approvals: { story_shape: true } })), true);
  assert.equal(storyDone(runState({ story_shape: STORY, passed_gates: ['story_shape'] })), true);
  assert.equal(storyDone(runState({ story_shape: STORY, current_gate: 'G1_SHOT_PLAN' })), true);
  assert.equal(storyDone(runState({ story_shape: STORY, passed_gates: ['G1_SHOT_PLAN'] })), true);
  assert.equal(storyDone(runState({ story_shape: STORY, stage: 'shot_planning' })), true);
  assert.equal(storyDone(null), false);
});

test('the Story panel locks once the story is done or the run has ended', () => {
  assert.equal(storyLocked(runState({ story_shape: STORY })), false);
  assert.equal(storyLocked(runState({ story_shape: STORY, current_gate: 'G1_SHOT_PLAN' })), true);
  assert.equal(storyLocked(runState({ story_shape: STORY, run: { ...OPEN_RUN, ended: true, outcome: 'failed' } })), true);
});

test('G1: characters in the shot plan without a locked look come first, as advice', () => {
  const cast = castView({ picks: { S01: ['mara'], S02: [{ character: 'theo', version: 2 }, 'mara'] } });
  const next = nextStep(runState({ current_gate: 'G1_SHOT_PLAN', story_shape: STORY }), null, cast);
  assert.equal(next.step, 'cast');
  assert.equal(next.title, 'Lock the look of Mara and Theo');
  assert.equal(next.href, '#cast');
  assert.equal(next.advisory, true);
  assert.deepEqual(next.also, { text: 'Go to the shot plan', href: '#shot-plan' });
});

test('G1: the version a shot picks is the one that must be locked', () => {
  // Theo's usual look (1) is locked, but S02 picks version 2, which is not
  const cast = castView({ locked: { 'mara:1': true, 'theo:1': true }, picks: { S01: ['mara', 'theo'], S02: [{ character: 'theo', version: 2 }] } });
  assert.deepEqual(charactersToLock(cast), ['Theo']);
  const allLocked = castView({ locked: { 'mara:1': true, 'theo:2': true }, picks: { S01: ['mara'], S02: [{ character: 'theo', version: 2 }] } });
  assert.deepEqual(charactersToLock(allLocked), []);
});

test('G1: a cast member in no shot is not a reason to wait', () => {
  const cast = castView({ locked: { 'mara:1': true }, picks: { S01: ['mara'] } });
  const next = nextStep(runState({ current_gate: 'G1_SHOT_PLAN' }), null, cast);
  assert.deepEqual([next.step, next.href, next.title], ['shot_plan', '#shot-plan', 'Approve the shot plan']);
});

test('G1 without a cast (or a cast this viewer may not see) is the shot plan', () => {
  assert.equal(nextStep(runState({ current_gate: 'G1_SHOT_PLAN' })).step, 'shot_plan');
  assert.equal(nextStep(runState({ current_gate: 'g1_shot_plan' }), null, { characters: [] }).step, 'shot_plan');
});

test('G2: review the pictures that have no decision yet', () => {
  const state = runState({
    current_gate: 'G2_SCENE_STILLS',
    shot_workflow_ids: { S01: 'wf-1', S02: 'wf-2', S03: 'wf-3' },
    shot_outcomes: { S01: { still_status: 'done' }, S02: { still_status: 'done' }, S03: { still_status: 'done' } },
    shot_decisions: { S02: 'approved' },
  });
  const next = nextStep(state);
  assert.deepEqual([next.step, next.title, next.href, next.count], ['stills', 'Review 2 scene pictures', '#scene-pictures', 2]);
  const one = nextStep({ ...state, shot_decisions: { S01: 'approved', S02: 'rejected' } });
  assert.equal(one.title, 'Review 1 scene picture');
  const none = nextStep({ ...state, shot_decisions: { S01: 'approved', S02: 'approved', S03: 'approved' } });
  assert.deepEqual([none.title, none.count], ['Approve all scene pictures', 0]);
});

test('shot ids are every shot the run knows, sorted', () => {
  assert.deepEqual(shotIds(runState({
    shot_workflow_ids: { S03: 'a' }, shot_outcomes: { S01: {} }, shot_decisions: { S02: 'approved', S01: 'approved' },
  })), ['S01', 'S02', 'S03']);
  assert.deepEqual(shotIds(null), []);
});

test('final cut gate', () => {
  const next = nextStep(runState({ current_gate: 'G_FINAL_CUT' }));
  assert.deepEqual([next.step, next.href], ['final_cut', '#final-cut']);
});

test('an unknown gate never shows its id', () => {
  const next = nextStep(runState({ current_gate: 'G7_SOMETHING' }));
  assert.equal(next.step, 'working');
  assert.doesNotMatch(`${next.title} ${next.text}`, /G7/);
});

test('a completed run is done; the finish card depends on the files', () => {
  const ended = { ended: true, outcome: 'completed', dry_run: false, can_start_again: false };
  const files = { phone: { available: true }, master_subs: { available: true } };
  const next = nextStep(runState({ current_gate: 'G_FINAL_CUT', run: ended }), null, null, files);
  assert.deepEqual([next.step, next.title, next.href], ['done', 'Your video is ready', '#finished']);
  assert.deepEqual(finishLine(ended, files), {
    ready: true, primary: 'phone', title: 'Your video is ready', text: 'Watch it here and download the files you need.',
  });
  assert.equal(finishLine(ended, { master_nosubs: { available: true } }).primary, 'master_nosubs');
  // a preview (dry run) has no files and says so instead of offering downloads
  const preview = finishLine({ ...ended, dry_run: true }, { phone: { available: false } });
  assert.deepEqual([preview.ready, preview.title], [false, 'Your preview is finished']);
  assert.equal(finishLine(ended, null).title, 'Your video is finished');
});

test('a run that stopped points at what went wrong', () => {
  const next = nextStep(runState({ run: { ended: true, outcome: 'failed', can_start_again: true } }));
  assert.deepEqual([next.step, next.href], ['failed', '#run-ended']);
  assert.match(next.text, /start it again/);
  assert.doesNotMatch(nextStep(runState({ run: { ended: true, outcome: 'cancelled', can_start_again: false } })).text, /start it again/);
});

test('the stepper marks done, current and skipped steps', () => {
  const statuses = (state, cast, story) => Object.fromEntries(
    stepperSteps(state, cast, nextStep(state, story, cast)).map((s) => [s.key, s.status]));

  assert.deepEqual(statuses(runState({ story_shape: STORY })), {
    story: 'current', cast: 'skipped', shot_plan: 'todo', stills: 'todo', final_cut: 'todo', download: 'todo',
  });

  const cast = castView({ picks: { S01: ['mara'] } });
  assert.deepEqual(statuses(runState({ story_shape: STORY, current_gate: 'G1_SHOT_PLAN', approvals: { story_shape: true } }), cast), {
    story: 'done', cast: 'current', shot_plan: 'todo', stills: 'todo', final_cut: 'todo', download: 'todo',
  });

  const g2 = runState({
    story_shape: STORY, current_gate: 'G2_SCENE_STILLS', passed_gates: ['story_shape', 'G1_SHOT_PLAN'],
    approvals: { story_shape: true, g1_shot_plan: true }, shot_workflow_ids: { S01: 'wf' },
  });
  // the look was never locked, and the video went past the shot plan without it
  assert.deepEqual(statuses(g2, cast), {
    story: 'done', cast: 'skipped', shot_plan: 'done', stills: 'current', final_cut: 'todo', download: 'todo',
  });

  const locked = castView({ locked: { 'mara:1': true }, picks: { S01: ['mara'] } });
  assert.equal(statuses(g2, locked).cast, 'done');

  // no story outline at all (story shaping off): skipped, never "current"
  assert.equal(statuses(runState({ stage: 'still_generation', passed_gates: ['G1_SHOT_PLAN'] })).story, 'skipped');

  // between steps the first open step is the one in progress
  assert.equal(statuses(runState({ story_shape: STORY, stage: 'clip_generation', approvals: { story_shape: true, g1_shot_plan: true, g2_scene_stills: true } })).final_cut, 'current');

  const done = runState({ story_shape: STORY, approvals: { story_shape: true }, run: { ended: true, outcome: 'completed' } });
  assert.deepEqual(statuses(done), {
    story: 'done', cast: 'skipped', shot_plan: 'done', stills: 'done', final_cut: 'done', download: 'current',
  });

  const stopped = runState({ story_shape: STORY, approvals: { story_shape: true, g1_shot_plan: true }, run: { ended: true, outcome: 'failed' } });
  assert.equal(statuses(stopped).stills, 'stopped');
});

test('studio home chips', () => {
  assert.deepEqual(statusChip({ step: 'stills' }), { label: 'Scene pictures to review', tone: 'todo' });
  assert.deepEqual(statusChip({ step: 'cast' }), { label: 'Shot plan to approve', tone: 'todo' });
  assert.deepEqual(statusChip({ step: 'done' }), { label: 'Finished', tone: 'ok' });
  assert.deepEqual(statusChip({ step: 'failed' }), { label: 'Stopped', tone: 'bad' });
  assert.deepEqual(statusChip(null), { label: 'In progress', tone: 'wait' });
});

test('names read as a sentence', () => {
  assert.equal(listNames([]), '');
  assert.equal(listNames(['Mara']), 'Mara');
  assert.equal(listNames(['Mara', 'Theo']), 'Mara and Theo');
  assert.equal(listNames(['Mara', 'Theo', 'Ivy']), 'Mara, Theo and Ivy');
  assert.equal(listNames(['Mara', 'Theo', 'Ivy', 'Bo']), 'Mara, Theo and 2 more');
});
