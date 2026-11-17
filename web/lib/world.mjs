/**
 * The studio home (web/app/studio/page.tsx) from GET /api/worlds and GET /api/episodes: the Universes,
 * then each episode in one section. Continue: not finished, in a Universe (or someone else's, whose
 * Universe the reader may not know). Recently finished: the run completed. Unfiled: the reader may
 * change it and it is in no Universe (so "Add to a Universe" applies).
 */

import { VIDEO_FILES } from './next-step.mjs';

/** Episode id -> the Universe card it is in, for the Universes the reader may see. */
export function universeByEpisode(worlds) {
  const out = new Map();
  for (const world of worlds ?? []) {
    for (const episodeId of world.episodes ?? []) {
      if (!out.has(episodeId)) out.set(episodeId, world);
    }
  }
  return out;
}

/** Whether the reader may change the episode: an admin, its owner, or anyone while it has no owner. */
export function mayChange(episode, viewer) {
  if (viewer?.isAdmin) return true;
  return episode?.user_id == null || (viewer?.userId != null && episode.user_id === viewer.userId);
}

/** The run completed (a finished video, or a finished preview). */
export function isFinished(state) {
  return Boolean(state?.run?.ended && state.run.outcome === 'completed');
}

/**
 * { continue, finished, unfiled }: each { episode, universe } in the order of `episodes` (newest
 * first). finished: the ids whose run completed (from each episode's run state, when read).
 * viewer: { userId, isAdmin }.
 */
export function homeSections(worlds, episodes, finished, viewer) {
  const byEpisode = universeByEpisode(worlds);
  const done = finished instanceof Set ? finished : new Set(finished ?? []);
  const sections = { continue: [], finished: [], unfiled: [] };
  for (const episode of episodes ?? []) {
    const universe = byEpisode.get(episode.episode_id) ?? null;
    const entry = { episode, universe };
    if (done.has(episode.episode_id)) sections.finished.push(entry);
    else if (!universe && mayChange(episode, viewer)) sections.unfiled.push(entry);
    else sections.continue.push(entry);
  }
  return sections;
}

/**
 * First run: no Universe to show and nothing of the reader's own yet (an admin: no episode at all), so
 * the home opens on creating a Universe. Other members' episodes do not count for a member.
 */
export function isFirstRun(worlds, episodes, viewer) {
  if ((worlds ?? []).length > 0) return false;
  const own = viewer?.isAdmin ? episodes ?? [] : (episodes ?? []).filter((e) => viewer?.userId != null && e.user_id === viewer.userId);
  return own.length === 0;
}

/** The Universes an episode may be added to: the ones the reader may change. */
export function universeChoices(worlds) {
  return (worlds ?? []).filter((w) => w.can_edit).map((w) => ({ series_id: w.series_id, name: w.name }));
}

/** The video file a finished card plays (GET /api/episodes/{id}/files): the phone version, else a master. */
export function primaryVideo(files) {
  return VIDEO_FILES.find((type) => files?.[type]?.available) ?? null;
}

/** The shot whose picture is a finished card's poster: the first shot (by id) that has a still. */
export function posterShot(shotOutcomes) {
  return Object.keys(shotOutcomes ?? {}).sort().find((sid) => shotOutcomes[sid]?.still_url) ?? null;
}

/**
 * What each scene picture was made with, by name (GET /api/episodes/{id}/shot-plan): its characters,
 * then its place, then its props, each once. Shots without any are left out; unknown picks too.
 */
export function madeWith(plan) {
  const characters = new Map((plan?.characters ?? []).map((c) => [c.slug, c.name]));
  const items = new Map((plan?.props ?? []).map((p) => [p.slug, p.name]));
  const out = {};
  for (const shot of plan?.shots ?? []) {
    const picks = [
      ...(shot.cast ?? []).map((entry) => characters.get(typeof entry === 'string' ? entry : entry?.character)),
      shot.place ? items.get(shot.place) : undefined,
      ...(shot.props ?? []).map((slug) => items.get(slug)),
    ];
    const names = [...new Set(picks.filter(Boolean))];
    if (names.length > 0) out[shot.shot_id] = names;
  }
  return out;
}

/** Where the browser keeps the start request of a Universe episode that waits for its looks to be locked. */
export function pendingStartKey(episodeId) {
  return `ddg:start:${episodeId}`;
}

/**
 * The start request (POST /api/episodes) the wizard kept for this episode, read from its stored text,
 * or null (nothing kept, unreadable, or another episode's). Without a readable dry_run it is a free
 * test run: a live run is only ever the one the wizard confirmed.
 */
export function pendingStart(text, episodeId) {
  let body;
  try {
    body = JSON.parse(text ?? '');
  } catch {
    return null;
  }
  if (!body || typeof body !== 'object' || body.episode_id !== episodeId) return null;
  const out = { episode_id: episodeId, dry_run: body.dry_run !== false, enable_story_shaping: body.enable_story_shaping !== false };
  if (typeof body.archetype_id === 'string' && body.archetype_id) out.archetype_id = body.archetype_id;
  if (typeof body.idea === 'string' && body.idea) out.idea = body.idea;
  if (typeof body.target_duration === 'number' && Number.isFinite(body.target_duration)) out.target_duration = body.target_duration;
  return out;
}
