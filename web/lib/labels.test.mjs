import test from 'node:test';
import assert from 'node:assert/strict';

import { importTs } from './ts-module.mjs';

const labels = await importTs(new URL('./labels.ts', import.meta.url));
const {
  gateLabel, stageLabel, qcLabel, shotStatusLabel, stillChip, fileLabel, roleLabel, ownerLabel,
  GATE_LABELS, STAGE_LABELS, QC_LABELS,
} = labels;

const RAW_ID = /^(G1_|G2_|G_|g1_|g2_|g_)|_/;

test('every review step has a plain name, whatever the case of its id', () => {
  assert.equal(gateLabel('G1_SHOT_PLAN'), 'Shot plan');
  assert.equal(gateLabel('g1_shot_plan'), 'Shot plan');
  assert.equal(gateLabel('G2_SCENE_STILLS'), 'Scene pictures');
  assert.equal(gateLabel('G_FINAL_CUT'), 'Final cut');
  assert.equal(gateLabel('story_shape'), 'Story');
  assert.equal(gateLabel(null), 'Nothing to review');
  // an unknown gate never shows its raw id
  assert.equal(gateLabel('G9_SOMETHING_NEW'), 'A review step');
});

test('the workflow stages (hfvg/models.py PipelineStage) all have plain names', () => {
  const stages = ['info_gathering', 'readback', 'script', 'character_locks', 'storyboard', 'shot_planning',
    'still_generation', 'still_review', 'clip_generation', 'clip_qc', 'mute_edit', 'picture_lock', 'audio',
    'final_mix', 'delivery', 'post_approval', 'complete'];
  assert.deepEqual(Object.keys(STAGE_LABELS).sort(), [...stages].sort());
  for (const stage of stages) assert.doesNotMatch(stageLabel(stage), RAW_ID, stage);
  assert.equal(stageLabel('still_generation'), 'Making the scene pictures');
  assert.equal(stageLabel(null), 'Getting started');
  assert.equal(stageLabel('a_new_stage'), 'Working on it');
});

test('QC verdicts read as plain words', () => {
  // every "verdict": "..." literal in hfvg/ and api/
  for (const verdict of ['PASS', 'WATCH', 'NEEDS_REVIEW', 'ESCALATE', 'FAIL', 'DRY_RUN', 'NO_CLIPS', 'NO_PICTURE', 'RESTORED']) {
    assert.ok(QC_LABELS[verdict], verdict);
    assert.doesNotMatch(qcLabel(verdict), RAW_ID, verdict);
  }
  assert.equal(qcLabel('DRY_RUN'), 'Not checked (preview run)');
  assert.equal(qcLabel('fail'), 'Needs a fix');
  assert.equal(qcLabel(undefined), 'Not checked yet');
  assert.equal(qcLabel('SOMETHING_ELSE'), 'Checked');
});

test('shot statuses read as plain words', () => {
  assert.equal(shotStatusLabel('done'), 'Ready to review');
  assert.equal(shotStatusLabel('failed'), "Couldn't be made");
  assert.equal(shotStatusLabel('approved'), 'Approved');
  assert.equal(shotStatusLabel('rejected'), 'Redo requested');
  assert.equal(shotStatusLabel(null), 'Waiting');
});

test('a scene picture chip puts the decision first, then the picture state', () => {
  assert.deepEqual(stillChip({ stillStatus: 'done' }), { label: 'Ready to review', tone: 'todo' });
  assert.deepEqual(stillChip({ stillStatus: 'done', decision: 'approved' }), { label: 'Approved', tone: 'ok' });
  assert.deepEqual(stillChip({ stillStatus: 'done', decision: 'rejected' }), { label: 'Redo requested', tone: 'warn' });
  assert.deepEqual(stillChip({ stillStatus: 'failed' }), { label: "Couldn't be made", tone: 'bad' });
  assert.deepEqual(stillChip({ stillStatus: 'pending' }), { label: 'Being made', tone: 'wait' });
  assert.deepEqual(stillChip({}), { label: 'Being made', tone: 'wait' });
  assert.deepEqual(stillChip({ stillStatus: 'done', decision: 'rejected', retrying: true }), { label: 'Being made again', tone: 'wait' });
  assert.equal(stillChip({ stillStatus: 'failed', held: true }).label, 'Held for an admin to check');
});

test('a picture the picture check failed reads as flagged until a decision is made on it', () => {
  assert.deepEqual(stillChip({ stillStatus: 'done', flagged: true }), { label: 'Flagged by the picture check', tone: 'warn' });
  // an approval made before the check failed it is not a decision on the flagged picture
  assert.equal(stillChip({ stillStatus: 'done', decision: 'approved', flagged: true }).label, 'Flagged by the picture check');
  assert.equal(stillChip({ stillStatus: 'done', decision: 'approved', flagged: false }).label, 'Approved');
  assert.equal(stillChip({ stillStatus: 'done', flagged: true, retrying: true }).label, 'Being made again');
  assert.equal(shotStatusLabel('qc_failed'), 'Flagged by the picture check');
});

test('once every scene picture is approved, a picture without its own decision is not still "to review"', () => {
  assert.deepEqual(stillChip({ stillStatus: 'done', allApproved: true }), { label: 'Approved', tone: 'ok' });
  assert.deepEqual(stillChip({ stillStatus: 'done', decision: 'rejected', allApproved: true }), { label: 'Redo requested', tone: 'warn' });
  assert.deepEqual(stillChip({ stillStatus: 'failed', allApproved: true }), { label: "Couldn't be made", tone: 'bad' });
  assert.deepEqual(stillChip({ stillStatus: 'pending', allApproved: true }), { label: 'Being made', tone: 'wait' });
});

test('gate names cover the three review gates the workflow parks at', () => {
  assert.deepEqual(Object.keys(GATE_LABELS).sort(), ['G1_SHOT_PLAN', 'G2_SCENE_STILLS', 'G_FINAL_CUT', 'STORY_SHAPE']);
});

test('files, roles and owners', () => {
  assert.equal(fileLabel('phone'), 'Phone version (9:16)');
  assert.equal(fileLabel('unknown_type'), 'unknown_type');
  assert.equal(roleLabel('admin'), 'Admin');
  assert.equal(roleLabel('member'), 'Member');
  assert.equal(ownerLabel('usr_1', 'Robin', 'usr_1'), 'You');
  assert.equal(ownerLabel('usr_2', 'Robin', 'usr_1'), 'Robin');
  assert.equal(ownerLabel('usr_2', null, 'usr_1'), 'Another member');
  assert.equal(ownerLabel(null, null, 'usr_1'), 'Shared');
  assert.equal(ownerLabel('usr_2', 'Robin', null), 'Robin');
});
