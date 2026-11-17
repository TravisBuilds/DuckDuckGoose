import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  homeSections, isFinished, isFirstRun, madeWith, mayChange, pendingStart, pendingStartKey, posterShot, primaryVideo,
  universeByEpisode, universeChoices,
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
