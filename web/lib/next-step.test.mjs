import test from 'node:test';
import assert from 'node:assert/strict';

import {
  cardStatus, charactersToLock, finishLine, gateChip, listNames, nextStep, openGate, shotIds, statusChip, stepperSteps,
  stillsToReview, storyDone, storyLocked, waitsOn,
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

test('G2: review the pictures that are ready and have no decision yet', () => {
  // the run records a shot's outcome only after its clip, so at G2 the shot states are the list
  const state = runState({ current_gate: 'G2_SCENE_STILLS', shots_count: 3, shot_decisions: { S02: 'approved' } });
  const shots = [
    { shot_id: 'S01', still_status: 'done' },
    { shot_id: 'S02', still_status: 'done' },
    { shot_id: 'S03', still_status: 'done' },
  ];
  const next = nextStep(state, null, null, { shots });
  assert.deepEqual([next.step, next.title, next.href, next.count], ['stills', 'Review 2 scene pictures', '#scene-pictures', 2]);
  const one = nextStep({ ...state, shot_decisions: { S01: 'approved', S02: 'rejected' } }, null, null, { shots });
  assert.equal(one.title, 'Review 1 scene picture');
  const none = nextStep({ ...state, shot_decisions: { S01: 'approved', S02: 'approved', S03: 'rejected' } }, null, null, { shots });
  assert.deepEqual([none.title, none.count], ['Approve all scene pictures', 0]);
});

test('G2: pictures still being made are counted apart; a failed one needs a decision too', () => {
  const state = runState({ current_gate: 'G2_SCENE_STILLS', shots_count: 4 });
  const shots = [
    { shot_id: 'S01', still_status: 'done' },
    { shot_id: 'S02', still_status: 'pending' },
    { shot_id: 'S03', still_status: 'failed' },
    { shot_id: 'S04', still_status: 'pending', pre_approved: true },
  ];
  assert.deepEqual(stillsToReview(state, shots), { review: 3, making: 1, total: 4 });
  const next = nextStep(state, null, null, { shots });
  assert.equal(next.title, 'Review 3 scene pictures');
  assert.match(next.text, /^1 more is still being made/);

  const making = nextStep(state, null, null, { shots: shots.map((s) => ({ ...s, still_status: 'pending', pre_approved: false })) });
  assert.deepEqual([making.step, making.title, making.text], ['stills', 'Making your scene pictures', '0 of 4 ready. They show up below as they finish.']);
});

test('G2 without shot states (the studio home) counts from the run state', () => {
  const state = runState({ current_gate: 'G2_SCENE_STILLS', shots_count: 4, shot_decisions: { S01: 'approved' } });
  assert.deepEqual(stillsToReview(state, null), { review: 3, making: 0, total: 4 });
  assert.equal(nextStep(state).title, 'Review 3 scene pictures');
});

test('shot ids are every shot the run state names, sorted', () => {
  assert.deepEqual(shotIds(runState({
    shot_workflow_ids: { S03: 'a' }, shot_outcomes: { S01: {} }, shot_decisions: { S02: 'approved', S01: 'approved' },
    failed_shots: [{ shot_id: 'S05', reason: 'x' }, 'S04'],
  })), ['S01', 'S02', 'S03', 'S04', 'S05']);
  assert.deepEqual(shotIds(null), []);
});

test('final cut gate', () => {
  const next = nextStep(runState({ current_gate: 'G_FINAL_CUT' }));
  assert.deepEqual([next.step, next.href], ['final_cut', '#final-cut']);
});

test('a passed or approved gate is not open, though the run keeps it as current_gate', () => {
  const approvals = { story_shape: true, g1_shot_plan: true, g2_scene_stills: false, g_final_cut: false };
  // G2 stays current_gate while the clips are made
  const clips = runState({ stage: 'clip_generation', current_gate: 'G2_SCENE_STILLS', passed_gates: ['G1_SHOT_PLAN', 'G2_SCENE_STILLS'],
    approvals: { ...approvals, g2_scene_stills: true } });
  assert.equal(openGate(clips), null);
  assert.deepEqual([nextStep(clips).step, nextStep(clips).title], ['working', 'Working on your video']);
  // approved, not yet recorded as passed
  const g1 = runState({ stage: 'g1_shot_plan', current_gate: 'G1_SHOT_PLAN', approvals });
  assert.equal(openGate(g1), null);
  assert.equal(nextStep(g1).step, 'working');
  assert.equal(openGate(runState({ current_gate: 'g2_scene_stills' })), 'G2_SCENE_STILLS');
  assert.equal(openGate(runState({ current_gate: 'G_FINAL_CUT', approvals: { g_final_cut: true } })), null);
  assert.equal(openGate(null), null);
});

test('an unknown gate never shows its id', () => {
  const next = nextStep(runState({ current_gate: 'G7_SOMETHING' }));
  assert.equal(next.step, 'working');
  assert.doesNotMatch(`${next.title} ${next.text}`, /G7/);
});

test('a completed run is done; the finish card depends on the files', () => {
  const ended = { ended: true, outcome: 'completed', dry_run: false, can_start_again: false };
  const files = { phone: { available: true }, master_subs: { available: true } };
  const next = nextStep(runState({ current_gate: 'G_FINAL_CUT', run: ended }), null, null, { files });
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

test('a purged run that finished reads as finished from its saved state', () => {
  const archived = {
    ended: true, outcome: 'completed', dry_run: true, can_start_again: false, archived: true, saved_at: '2026-11-14 10:40:00',
  };
  const files = { phone: { available: true }, master_subs: { available: true }, subtitles_srt: { available: true } };
  const state = runState({
    stage: 'mute_edit', story_shape: STORY, current_gate: 'G_FINAL_CUT',
    passed_gates: ['STORY_SHAPE', 'G1_SHOT_PLAN', 'G2_SCENE_STILLS', 'G_FINAL_CUT'],
    approvals: { story_shape: true, g1_shot_plan: true, g2_scene_stills: true, g_final_cut: true },
    shot_outcomes: { S01: { status: 'completed' } }, shot_decisions: { S01: 'approved' }, run: archived,
  });
  const story = { story_shape: STORY, approved: true };
  const next = nextStep(state, story, null, { files });
  assert.deepEqual([next.step, next.title, next.href], ['done', 'Your video is ready', '#finished']);
  assert.deepEqual(statusChip(next), { label: 'Finished', tone: 'ok' });
  assert.deepEqual(Object.fromEntries(stepperSteps(state, story, null, next).map((s) => [s.key, s.status])), {
    story: 'done', cast: 'skipped', shot_plan: 'done', stills: 'done', final_cut: 'done', download: 'current',
  });
  assert.equal(storyLocked(state), true);
  assert.equal(openGate(state), null);
});

test('a purged run nothing saved finishes: its history expired, and the files it left are shown', () => {
  const expired = {
    ended: true, outcome: 'expired', dry_run: true, can_start_again: false, archived: true, saved_at: null,
    reason: 'How this run ended is no longer known: no finished state was saved for it.',
  };
  const gone = runState({ stage: null, run: expired });
  const files = { phone: { available: false }, master_nosubs: { available: true }, subtitles_srt: { available: true } };
  const next = nextStep(gone, null, null, { files });
  assert.deepEqual([next.step, next.title, next.href], ['expired', "This run's history expired", '#finished']);
  assert.match(next.text, /video it delivered is here to watch and download/);
  assert.deepEqual(finishLine(expired, files), {
    ready: true, primary: 'master_nosubs', title: "This run's history expired",
    text: 'The studio no longer knows how the run ended, but the video it delivered is here to watch and download.',
  });
  const bare = finishLine(expired, { subtitles_srt: { available: true } });
  assert.deepEqual([bare.ready, bare.primary], [false, null]);
  assert.match(bare.text, /no video file is left for it/);
  assert.match(nextStep(gone, null, null, { files: null }).text, /no video file is left for it/);
  assert.deepEqual(statusChip(next), { label: 'History expired', tone: 'warn' });
  // nothing shows which steps it took, so no stepper, and nothing is anyone's to do
  assert.equal(stepperSteps(gone, null, null, next), null);
  const theirs = nextStep(gone, null, null, { files, viewer: { mine: false, canAct: false, ownerName: 'Robin' } });
  assert.deepEqual([theirs.step, theirs.waiting], ['expired', undefined]);
  assert.equal(storyLocked(gone), true);
});

test('no run state at all, but delivered files: the files, never "hasn\'t started"', () => {
  const files = { phone: { available: true } };
  const next = nextStep(null, null, null, { files });
  assert.deepEqual([next.step, next.title, next.href], ['expired', "This run's history could not be loaded", '#finished']);
  assert.deepEqual(finishLine(null, files).primary, 'phone');
  // a subtitle file alone is no video to show
  assert.equal(nextStep(null, null, null, { files: { subtitles_srt: { available: true } } }).step, 'working');
  assert.equal(nextStep(null, null, null, { files: { phone: { available: false } } }).step, 'working');
});

// GET /api/episodes/{id} for a delivered run whose final cut was approved a day before Temporal purged
// it (the ep98cle case): the state the closed run reported, from its saved snapshot, run archived.
const PURGED_DELIVERED = {
  episode_id: 'ep98cle', stage: 'picture_lock', current_gate: 'G_FINAL_CUT',
  passed_gates: ['G1_SHOT_PLAN', 'G2_SCENE_STILLS', 'G_FINAL_CUT'],
  approvals: { story_shape: true, g1_shot_plan: true, g2_scene_stills: true, g_final_cut: true },
  shots_count: 1, shot_decisions: {}, pending_retries: [], pending_clip_retries: [], failed_shots: [],
  shot_outcomes: { A01: { status: 'completed', still_url: 'still.jpg', clip_url: 'clip.mp4', pre_approved: false } },
  run: {
    run_id: 'run-1', status: 'ARCHIVED', ended: true, outcome: 'completed', reason: null, dry_run: false,
    can_start_again: false, archived: true, saved_at: '2026-11-15 16:15:00',
  },
};
const DELIVERED = {
  master_subs: { available: true }, master_nosubs: { available: true }, phone: { available: true },
  subtitles_srt: { available: true }, mute_edit: { available: true },
};

test('episode page of a delivered run Temporal purged: finished, with its video, never "hasn\'t started"', () => {
  const next = nextStep(PURGED_DELIVERED, null, null, { files: DELIVERED });
  assert.deepEqual([next.step, next.title, next.href], ['done', 'Your video is ready', '#finished']);
  assert.doesNotMatch(`${next.title} ${next.text}`, /hasn't started/);
  assert.deepEqual(finishLine(PURGED_DELIVERED.run, DELIVERED), {
    ready: true, primary: 'phone', title: 'Your video is ready', text: 'Watch it here and download the files you need.',
  });
  assert.deepEqual(Object.fromEntries(stepperSteps(PURGED_DELIVERED, null, null, next).map((s) => [s.key, s.status])), {
    story: 'skipped', cast: 'skipped', shot_plan: 'done', stills: 'done', final_cut: 'done', download: 'current',
  });
  assert.equal(openGate(PURGED_DELIVERED), null);
  // someone else's finished video waits on no one either
  const theirs = nextStep(PURGED_DELIVERED, null, null, { files: DELIVERED, viewer: { mine: false, canAct: false, ownerName: 'Robin' } });
  assert.deepEqual([theirs.step, theirs.waiting], ['done', undefined]);
});

test('studio home card: "Finished" for a delivered run Temporal purged, "Not started" only without a run', () => {
  const mine = { mine: true, canAct: true, ownerName: null };
  const theirs = { mine: false, canAct: false, ownerName: 'Robin' };
  assert.deepEqual(cardStatus(PURGED_DELIVERED, null, mine), { label: 'Finished', tone: 'ok' });
  assert.deepEqual(cardStatus(PURGED_DELIVERED, null, theirs), { label: 'Finished', tone: 'ok' });
  assert.deepEqual(cardStatus(PURGED_DELIVERED, { story_shape: STORY, approved: true }, theirs), { label: 'Finished', tone: 'ok' });
  const expired = { ...PURGED_DELIVERED, run: { ...PURGED_DELIVERED.run, outcome: 'expired', saved_at: null } };
  assert.deepEqual(cardStatus(expired, null, mine), { label: 'History expired', tone: 'warn' });
  assert.deepEqual(cardStatus(null, null, mine), { label: 'Not started', tone: 'wait' });
  assert.deepEqual(cardStatus(undefined), { label: 'Not started', tone: 'wait' });
  // a run still going is not finished just because files exist
  const open = { ...PURGED_DELIVERED, passed_gates: ['G1_SHOT_PLAN', 'G2_SCENE_STILLS'],
    approvals: { ...PURGED_DELIVERED.approvals, g_final_cut: false }, run: OPEN_RUN };
  assert.deepEqual(cardStatus(open, null, mine), { label: 'Final cut to approve', tone: 'todo' });
});

test('a run that stopped points at what went wrong', () => {
  const next = nextStep(runState({ run: { ended: true, outcome: 'failed', can_start_again: true } }));
  assert.deepEqual([next.step, next.href], ['failed', '#run-ended']);
  assert.match(next.text, /start it again/);
  assert.doesNotMatch(nextStep(runState({ run: { ended: true, outcome: 'cancelled', can_start_again: false } })).text, /start it again/);
});

test('the stepper marks done, current and skipped steps', () => {
  const statuses = (state, cast, story) => Object.fromEntries(
    stepperSteps(state, story, cast, nextStep(state, story, cast)).map((s) => [s.key, s.status]));

  assert.deepEqual(statuses(runState({ story_shape: STORY })), {
    story: 'current', cast: 'skipped', shot_plan: 'todo', stills: 'todo', final_cut: 'todo', download: 'todo',
  });
  // the API's run state has no outline: the one from GET /story-shape decides between done and skipped
  const approvedNoOutline = runState({ stage: 'still_generation', approvals: { story_shape: true } });
  assert.equal(statuses(approvedNoOutline, null, { story_shape: STORY }).story, 'done');
  assert.equal(statuses(approvedNoOutline, null, { story_shape: null }).story, 'skipped');

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
  assert.deepEqual(statusChip({ step: 'expired' }), { label: 'History expired', tone: 'warn' });
  assert.deepEqual(statusChip({ step: 'failed' }), { label: 'Stopped', tone: 'bad' });
  assert.deepEqual(statusChip(null), { label: 'In progress', tone: 'wait' });
});

test("someone else's video: each step says who it waits on, never an instruction they cannot follow", () => {
  const other = { viewer: { mine: false, canAct: false, ownerName: 'Robin' } };
  const story = nextStep(runState({ story_shape: STORY }), { story_shape: STORY, approved: false }, null, other);
  assert.deepEqual([story.step, story.title, story.href, story.waiting, story.waitingOn],
    ['story', 'Waiting on Robin to approve the story', '#story', true, 'Robin']);
  assert.match(story.text, /Only Robin or an admin can change or approve it/);

  // the cast advice is for the owner: someone else waits on the shot plan
  const cast = castView({ picks: { S01: ['mara'] } });
  const g1 = nextStep(runState({ current_gate: 'G1_SHOT_PLAN' }), null, cast, other);
  assert.deepEqual([g1.step, g1.title, g1.href, g1.advisory], ['shot_plan', 'Waiting on Robin to approve the shot plan', '#shot-plan', undefined]);
  assert.doesNotMatch(g1.text, /^Look over the shots, then approve/);

  const g2 = runState({ current_gate: 'G2_SCENE_STILLS', shot_decisions: { S01: 'approved' } });
  const shots = ['S01', 'S02', 'S03'].map((shot_id) => ({ shot_id, still_status: 'done' }));
  const review = nextStep(g2, null, null, { ...other, shots });
  assert.deepEqual([review.step, review.title, review.count], ['stills', 'Waiting on Robin to review 2 scene pictures', 2]);
  const allDecided = nextStep({ ...g2, shot_decisions: { S01: 'approved', S02: 'approved', S03: 'rejected' } }, null, null, { ...other, shots });
  assert.equal(allDecided.title, 'Waiting on Robin to approve all scene pictures');
  // pictures still being made are progress, not a step: no one is waited on, and it is not "your" video
  const making = nextStep(g2, null, null, { ...other, shots: shots.map((s) => ({ ...s, still_status: 'pending' })) });
  assert.deepEqual([making.title, making.waiting], ['Making the scene pictures', undefined]);

  const cut = nextStep(runState({ current_gate: 'G_FINAL_CUT' }), null, null, other);
  assert.deepEqual([cut.step, cut.title, cut.href], ['final_cut', 'Waiting on Robin to approve the final cut', '#final-cut']);

  const stopped = nextStep(runState({ run: { ended: true, outcome: 'failed', can_start_again: true } }), null, null, other);
  assert.equal(stopped.text, 'See what went wrong. Only Robin or an admin can start it again.');
  const done = nextStep(runState({ run: { ended: true, outcome: 'completed', dry_run: false } }), null, null,
    { ...other, files: { phone: { available: true } } });
  assert.equal(done.title, 'This video is ready');
  assert.equal(finishLine({ ended: true, dry_run: true }, null, false).title, 'This preview is finished');
  assert.equal(nextStep(runState({ stage: 'clip_generation', passed_gates: ['G1_SHOT_PLAN'] }), null, null, other).title,
    'Working on this video');

  // without the owner's name
  const unnamed = nextStep(runState({ current_gate: 'G1_SHOT_PLAN' }), null, null, { viewer: { mine: false, canAct: false } });
  assert.equal(unnamed.title, 'Waiting on the owner to approve the shot plan');
});

test('an admin on someone else\'s video, or anyone on a video without an owner, takes the step', () => {
  const admin = { viewer: { mine: false, canAct: true, ownerName: 'Robin' } };
  const story = nextStep(runState({ story_shape: STORY }), { story_shape: STORY, approved: false }, null, admin);
  assert.deepEqual([story.title, story.waiting], ['Read and approve the story', undefined]);
  assert.equal(nextStep(runState({ current_gate: 'G1_SHOT_PLAN' }), null, null, admin).title, 'Approve the shot plan');
  assert.match(nextStep(runState({ run: { ended: true, outcome: 'failed', can_start_again: true } }), null, null, admin).text,
    /then start it again/);
  // a video without an owner is everyone's: it reads as the viewer's own
  const shared = { viewer: { mine: true, canAct: true, ownerName: null } };
  assert.equal(nextStep(runState({ story_shape: STORY }), { story_shape: STORY }, null, shared).title, 'Read and approve your story');
});

test('home chips say who someone else\'s video waits on', () => {
  const other = { viewer: { mine: false, canAct: false, ownerName: 'Robin' } };
  assert.deepEqual(statusChip(nextStep(runState({ current_gate: 'G1_SHOT_PLAN' }), null, null, other)),
    { label: 'Waiting on Robin', tone: 'wait' });
  assert.deepEqual(statusChip(nextStep(runState({ run: { ended: true, outcome: 'completed' } }), null, null, other)),
    { label: 'Finished', tone: 'ok' });
  assert.deepEqual(statusChip(nextStep(runState({ current_gate: 'G1_SHOT_PLAN' }), null, null,
    { viewer: { mine: false, canAct: true, ownerName: 'Robin' } })), { label: 'Shot plan to approve', tone: 'todo' });
});

test('a decision\'s section says who it waits on to someone who may not take it', () => {
  // the owner, an admin, anyone on a video without an owner, and a reader not known yet take it
  for (const viewer of [{ mine: true, canAct: true, ownerName: 'Robin' }, { mine: false, canAct: true, ownerName: 'Robin' },
    { mine: true, canAct: true, ownerName: null }, null]) {
    assert.equal(waitsOn(viewer), null);
    assert.deepEqual(gateChip(false, true, waitsOn(viewer)), { label: 'Waiting for you', tone: 'todo' });
  }
  // another member: neutral, naming the owner when known
  const other = waitsOn({ mine: false, canAct: false, ownerName: 'Robin' });
  assert.equal(other, 'Robin');
  assert.deepEqual(gateChip(false, true, other), { label: 'Waiting on Robin', tone: 'wait' });
  assert.deepEqual(gateChip(false, true, waitsOn({ mine: false, canAct: false })), { label: 'Waiting on the owner', tone: 'wait' });
  // approved, or nothing to decide, reads the same to everyone
  for (const who of [null, other]) {
    assert.deepEqual(gateChip(true, false, who), { label: 'Approved', tone: 'ok' });
    assert.deepEqual(gateChip(true, true, who), { label: 'Approved', tone: 'ok' });
    assert.equal(gateChip(false, false, who), null);
  }
});

test('names read as a sentence', () => {
  assert.equal(listNames([]), '');
  assert.equal(listNames(['Mara']), 'Mara');
  assert.equal(listNames(['Mara', 'Theo']), 'Mara and Theo');
  assert.equal(listNames(['Mara', 'Theo', 'Ivy']), 'Mara, Theo and Ivy');
  assert.equal(listNames(['Mara', 'Theo', 'Ivy', 'Bo']), 'Mara, Theo and 2 more');
});
