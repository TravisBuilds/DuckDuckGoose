/**
 * The studio home (web/app/studio/page.tsx) from GET /api/worlds and GET /api/episodes: the Universes,
 * then each episode in one section. Continue: not finished, in a Universe (or someone else's, whose
 * Universe the reader may not know). Recently finished: the run completed. Unfiled: the reader may
 * change it and it is in no Universe (so "Add to a Universe" applies).
 */

import { formatUsdMicros } from './balance.mjs';
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

/** How often the Universe page asks again while a live mood board is being made (ms). */
export const BOARD_POLL_MS = 4000;

/**
 * What the Universe page says about a live mood board (creation.board_request), or null when there
 * is nothing to say. making: the pictures made so far (the page asks again every BOARD_POLL_MS);
 * failed: no picture was made, with the reason; short: the latest board has fewer pictures than
 * asked (the ones that were paid for), with the reason.
 */
export function boardProgress(request) {
  if (!request || typeof request !== 'object') return null;
  const asked = Number.isInteger(request.image_count) && request.image_count > 0 ? request.image_count : 0;
  const made = Number.isInteger(request.made) && request.made > 0 ? Math.min(request.made, asked) : 0;
  const reason = typeof request.error === 'string' ? request.error.trim() : '';
  if (request.status === 'making') {
    return { kind: 'making', made, asked, text: `Making the mood board: ${made} of ${asked} pictures made. This page updates by itself.` };
  }
  if (request.status === 'failed') {
    return { kind: 'failed', made, asked, text: reason ? `The mood board was not made. ${reason}` : 'The mood board was not made.' };
  }
  if (request.status === 'done' && reason && made < asked) {
    return { kind: 'short', made, asked, text: `This mood board has ${made} of the ${asked} pictures. ${reason}` };
  }
  return null;
}

/**
 * What a live mood board estimate (POST /api/worlds/{id}/board/estimate) shows before anything is
 * spent, after its summary: the price of each picture, what the pictures are made with, and what the
 * Universe's mood board budget has left before its stop. A dry-run estimate: none.
 */
export function boardEstimateLines(estimate) {
  if (!estimate || estimate.dry_run) return [];
  const prices = estimate.per_image_usd_micros ?? [];
  const lines = [];
  if (prices.length > 0) {
    lines.push(prices.every((p) => p === prices[0])
      ? `${prices.length} picture${prices.length === 1 ? '' : 's'} at ${formatUsdMicros(prices[0])} each (the provider's price)`
      : `The provider's price of each picture: ${prices.map(formatUsdMicros).join(', ')}`);
  }
  const refs = Number.isInteger(estimate.references_per_picture) ? estimate.references_per_picture : 0;
  const madeFrom = refs > 0 ? `your notes and ${refs} of your reference photos` : 'your notes';
  lines.push(estimate.model_path ? `Made from ${madeFrom}, with ${estimate.model_path}` : `Made from ${madeFrom}`);
  const budget = estimate.budget;
  if (budget) {
    lines.push(`This Universe's mood board budget has ${formatUsdMicros(budget.available_usd_micros)} left `
      + `(it stops at ${formatUsdMicros(budget.stop_usd_micros)} spent of ${formatUsdMicros(budget.cap_usd_micros)})`);
  }
  return lines;
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
