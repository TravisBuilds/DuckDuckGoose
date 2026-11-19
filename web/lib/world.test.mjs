import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  BOARD_POLL_MS, boardEstimateLines, boardProgress, homeSections, isFinished, isFirstRun, madeWith, mayChange,
  pendingStart, pendingStartKey, posterShot, primaryVideo, startRequest, universeByEpisode, universeChoices,
} from './world.mjs';

const worlds = [
  { series_id: 'se_harbour', name: 'Harbour Lights', can_edit: true, episodes: ['ep01', 'ep02', 'ep03'] },
  { series_id: 'se_orchard', name: 'Orchard Days', can_edit: false, episodes: ['ep04'] },
];
const ep = (episode_id, user_id) => ({ episode_id, user_id, owner_name: user_id ? `Name ${user_id}` : null });
const me = { userId: 'u_me', isAdmin: false };

test('each episode is in one section: finished, in a Universe, or unfiled', () => {
  const episodes = [
    ep('ep01', 'u_me'), ep('ep02', 'u_me'), ep('ep04', 'u_other'), ep('ep05', 'u_me'), ep('ep06', null), ep('ep07', 'u_other'),
    ep('ep08', 'u_me'),
  ];
  const sections = homeSections(worlds, episodes, new Set(['ep02', 'ep08']), me);
  const ids = (list) => list.map((e) => e.episode.episode_id);
  assert.deepEqual(ids(sections.finished), ['ep02', 'ep08']);
  assert.deepEqual(ids(sections.continue), ['ep01', 'ep04', 'ep07']);
  assert.deepEqual(ids(sections.unfiled), ['ep05', 'ep06']);
  // the Universe comes with its card; a finished episode in no Universe has none
  assert.equal(sections.finished[0].universe.name, 'Harbour Lights');
  assert.equal(sections.finished[1].universe, null);
  assert.equal(sections.continue[1].universe.series_id, 'se_orchard');
  // someone else's episode whose Universe the reader may not know is not "in no Universe"
  assert.equal(sections.continue[2].universe, null);
  // nothing is listed twice or left out
  const all = [...sections.continue, ...sections.finished, ...sections.unfiled].map((e) => e.episode.episode_id).sort();
  assert.deepEqual(all, episodes.map((e) => e.episode_id).sort());
});

test('an admin may file anyone\'s episode; finished ids may come as a list', () => {
  const sections = homeSections(worlds, [ep('ep07', 'u_other'), ep('ep03', 'u_other')], ['ep03'], { userId: 'u_admin', isAdmin: true });
  assert.deepEqual(sections.unfiled.map((e) => e.episode.episode_id), ['ep07']);
  assert.deepEqual(sections.finished.map((e) => e.episode.episode_id), ['ep03']);
  assert.deepEqual(homeSections(null, null, null, null), { continue: [], finished: [], unfiled: [] });
});

test('who may change an episode, and when a run is finished', () => {
  assert.equal(mayChange(ep('ep01', 'u_me'), me), true);
  assert.equal(mayChange(ep('ep01', null), me), true);
  assert.equal(mayChange(ep('ep01', 'u_other'), me), false);
  assert.equal(mayChange(ep('ep01', 'u_other'), { userId: 'u_x', isAdmin: true }), true);
  assert.equal(mayChange(ep('ep01', 'u_other'), null), false);
  assert.equal(mayChange(ep('ep01', 'u_me'), { userId: null, isAdmin: false }), false);
  assert.equal(isFinished({ run: { ended: true, outcome: 'completed' } }), true);
  assert.equal(isFinished({ run: { ended: true, outcome: 'failed' } }), false);
  assert.equal(isFinished({ run: { ended: true, outcome: 'expired' } }), false);
  assert.equal(isFinished({ run: { ended: false } }), false);
  assert.equal(isFinished(null), false);
});

test('the home opens on making a Universe when there is nothing to show yet', () => {
  assert.equal(isFirstRun([], [], me), true);
  // other members' videos do not count for a member, their own do
  assert.equal(isFirstRun([], [ep('ep07', 'u_other')], me), true);
  assert.equal(isFirstRun([], [ep('ep07', 'u_other'), ep('ep05', 'u_me')], me), false);
  assert.equal(isFirstRun([worlds[0]], [], me), false);
  // an admin looks after everyone's videos
  assert.equal(isFirstRun([], [ep('ep07', 'u_other')], { userId: 'u_admin', isAdmin: true }), false);
  assert.equal(isFirstRun(null, null, null), true);
});

test('Universes an episode may join, the file a finished card plays and its poster', () => {
  assert.deepEqual(universeChoices(worlds), [{ series_id: 'se_harbour', name: 'Harbour Lights' }]);
  assert.deepEqual(universeChoices(null), []);
  assert.equal(universeByEpisode(worlds).get('ep04').name, 'Orchard Days');
  assert.equal(primaryVideo({ phone: { available: false }, master_subs: { available: true }, srt: { available: true } }), 'master_subs');
  assert.equal(primaryVideo({ phone: { available: true }, master_subs: { available: true } }), 'phone');
  assert.equal(primaryVideo({ srt: { available: true } }), null);
  assert.equal(primaryVideo(null), null);
  assert.equal(posterShot({ B01: { still_url: '/b' }, A02: { still_url: '/a2' }, A01: { still_url: null } }), 'A02');
  assert.equal(posterShot({ A01: {} }), null);
  assert.equal(posterShot(null), null);
});

test('what each scene picture was made with', () => {
  const plan = {
    characters: [{ slug: 'mira-vale', name: 'Mira Vale' }, { slug: 'oskar', name: 'Oskar' }],
    props: [{ slug: 'harbour', name: 'The Harbour' }, { slug: 'lantern', name: 'Brass lantern' }],
    shots: [
      { shot_id: 'S01', cast: ['mira-vale', { character: 'oskar', version: 2 }], place: 'harbour', props: ['lantern'] },
      { shot_id: 'S02', cast: [{ character: 'mira-vale' }, 'mira-vale', 'gone'], place: null, props: ['lantern', 'lantern'] },
      { shot_id: 'S03', cast: null, place: 'nowhere', props: null },
      { shot_id: 'S04' },
    ],
  };
  assert.deepEqual(madeWith(plan), {
    S01: ['Mira Vale', 'Oskar', 'The Harbour', 'Brass lantern'],
    S02: ['Mira Vale', 'Brass lantern'],
  });
  assert.deepEqual(madeWith(null), {});
  assert.deepEqual(madeWith({ shots: [{ shot_id: 'S01', cast: ['mira-vale'] }] }), {});
});

test('what the page says while a live mood board is made, and when it stopped', () => {
  const request = (status, made, error = null) => ({ status, made, error, image_count: 4, board_id: status === 'done' ? 'mb_1' : null });
  assert.deepEqual(boardProgress(request('making', 1)), {
    kind: 'making', made: 1, asked: 4, text: 'Making the mood board: 1 of 4 pictures made. This page updates by itself.',
  });
  assert.ok(BOARD_POLL_MS >= 1000 && BOARD_POLL_MS <= 10_000);
  const stop = 'The price of a picture went up after you approved it, so the studio stopped before paying more.';
  assert.deepEqual(boardProgress(request('failed', 0, stop)), {
    kind: 'failed', made: 0, asked: 4, text: `The mood board was not made. ${stop}`,
  });
  assert.equal(boardProgress(request('failed', 0)).text, 'The mood board was not made.');
  // a board with fewer pictures than asked (the ones that were paid for) says why
  assert.deepEqual(boardProgress(request('done', 3, stop)), {
    kind: 'short', made: 3, asked: 4, text: `This mood board has 3 of the 4 pictures. ${stop}`,
  });
  // a finished board, nothing at all, or a count that makes no sense
  assert.equal(boardProgress(request('done', 4)), null);
  assert.equal(boardProgress(request('done', 4, stop)), null);
  assert.equal(boardProgress(null), null);
  assert.equal(boardProgress({ status: 'making', made: 9, image_count: 4 }).made, 4);
  assert.equal(boardProgress({ status: 'making', made: -1, image_count: 'x' }).text,
    'Making the mood board: 0 of 0 pictures made. This page updates by itself.');
});

test('a live mood board estimate shows each picture\'s price, what it is made with and the budget left', () => {
  const live = {
    dry_run: false, per_image_usd_micros: [390_000, 390_000, 390_000, 390_000], total_usd_micros: 1_560_000,
    model_path: 'xai/grok-imagine-image-2.0', references_per_picture: 2,
    budget: { line: 'U1_mood_board', cap_usd_micros: 4_000_000, stop_usd_micros: 3_200_000, spent_usd_micros: 390_000,
      reserved_usd_micros: 0, available_usd_micros: 2_810_000 },
  };
  assert.deepEqual(boardEstimateLines(live), [
    "4 pictures at $0.39 each (the provider's price)",
    'Made from your notes and 2 of your reference photos, with xai/grok-imagine-image-2.0',
    "This Universe's mood board budget has $2.81 left (it stops at $3.20 spent of $4.00)",
  ]);
  assert.deepEqual(boardEstimateLines({ ...live, per_image_usd_micros: [390_000, 410_000], references_per_picture: 1, budget: undefined }), [
    "The provider's price of each picture: $0.39, $0.41",
    'Made from your notes and 1 of your reference photos, with xai/grok-imagine-image-2.0',
  ]);
  assert.deepEqual(boardEstimateLines({ ...live, per_image_usd_micros: [], references_per_picture: 0, model_path: undefined,
    budget: undefined }), ['Made from your notes']);
  assert.deepEqual(boardEstimateLines({ ...live, dry_run: true }), []);
  assert.deepEqual(boardEstimateLines(null), []);
});

test('the start request kept while a Universe episode waits for its looks', () => {
  assert.equal(pendingStartKey('ep_harbour_01'), 'ddg:start:ep_harbour_01');
  const sent = {
    episode_id: 'ep_harbour_01', dry_run: false, enable_story_shaping: true, archetype_id: 'cozy-slice',
    idea: 'A lighthouse keeper finds a map', target_duration: 30,
  };
  assert.deepEqual(pendingStart(JSON.stringify(sent), 'ep_harbour_01'), sent);
  // another episode's request, nothing kept, or text that is not a request: nothing to start with
  assert.equal(pendingStart(JSON.stringify(sent), 'ep_other'), null);
  assert.equal(pendingStart(null, 'ep_harbour_01'), null);
  assert.equal(pendingStart('{not json', 'ep_harbour_01'), null);
  assert.equal(pendingStart('"ep_harbour_01"', 'ep_harbour_01'), null);
  // only a run the wizard confirmed as live is live; unknown fields stay behind
  assert.deepEqual(pendingStart(JSON.stringify({ episode_id: 'ep_harbour_01', archetype_id: null, budget: 9 }), 'ep_harbour_01'),
    { episode_id: 'ep_harbour_01', dry_run: true, enable_story_shaping: true });
  assert.equal(pendingStart(JSON.stringify({ ...sent, dry_run: 'no' }), 'ep_harbour_01').dry_run, true);
});

test('the start request the studio kept reads the same way on any device', () => {
  const kept = {
    episode_id: 'ep_harbour_01', dry_run: false, enable_story_shaping: true, archetype_id: 'cozy-slice',
    idea: 'A lighthouse keeper finds a map', target_duration: 30,
  };
  assert.deepEqual(startRequest(kept, 'ep_harbour_01'), kept);
  assert.deepEqual(startRequest(kept, 'ep_harbour_01'), pendingStart(JSON.stringify(kept), 'ep_harbour_01'));
  // nothing kept, another episode's, or not a request: nothing to start with
  assert.equal(startRequest(null, 'ep_harbour_01'), null);
  assert.equal(startRequest(undefined, 'ep_harbour_01'), null);
  assert.equal(startRequest(kept, 'ep_other'), null);
  assert.equal(startRequest('ep_harbour_01', 'ep_harbour_01'), null);
  // live only when it says so; unknown fields stay behind
  assert.deepEqual(startRequest({ episode_id: 'ep_harbour_01', dry_run: 'false', budget: 9 }, 'ep_harbour_01'),
    { episode_id: 'ep_harbour_01', dry_run: true, enable_story_shaping: true });
});
