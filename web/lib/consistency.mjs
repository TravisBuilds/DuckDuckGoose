/**
 * Consistency signals of a Universe (docs/WORLDS.md), shared by the studio home, the Universe page, the
 * create wizard and the episode page: the badge of a character version, a prop or a place, the
 * Universe's health line, "Used in N episodes", and the drafted characters that are the Universe's own
 * (matched by name). Shapes: GET /api/worlds, GET /api/worlds/{id}, GET /api/episodes/{id}/universe.
 */

const BADGES = {
  drift: { key: 'drift', label: 'Look changed since lock', tone: 'bad' },
  older: { key: 'older', label: 'Older look', tone: 'warn' },
  unlocked: { key: 'unlocked', label: 'Not locked yet', tone: 'wait' },
  photo: { key: 'photo', label: 'Photo set', tone: 'ok' },
  noPhoto: { key: 'no-photo', label: 'No photo', tone: 'wait' },
};

/**
 * The badge of one character version: { key, label, tone }. version: { version, locked, problems? }
 * (problems: what changed in its locked pictures since the lock). latestLocked: the newest locked
 * version of the character, or null. A changed lock comes first, then an older look than the newest
 * locked one, then the lock itself.
 */
export function characterBadge(version, latestLocked = null) {
  if (!version) return BADGES.unlocked;
  if (version.locked && (version.problems ?? []).length > 0) return BADGES.drift;
  if (latestLocked != null && version.version < latestLocked) return BADGES.older;
  if (version.locked) return { key: 'locked', label: `Locked v${version.version}`, tone: 'ok' };
  return BADGES.unlocked;
}

function newestLocked(versions) {
  return (versions ?? []).filter((v) => v.locked).reduce((a, b) => (!a || b.version > a.version ? b : a), null);
}

/** A character's own badge: its newest locked look (a changed lock shows), else "Not locked yet". */
export function characterSummaryBadge(character) {
  const latest = newestLocked(character?.versions);
  return latest ? characterBadge(latest, latest.version) : BADGES.unlocked;
}

/** The badge of a character in an episode's strip (GET /api/episodes/{id}/universe strip.characters). */
export function stripBadge(entry) {
  return characterBadge(entry, entry?.latest_locked ?? null);
}

/** A prop's or place's badge: phase 1 has no locks for them, only whether they have a photo. */
export function itemBadge(item) {
  const photos = typeof item?.photos === 'number' ? item.photos : (item?.photos ?? []).length;
  return photos > 0 || item?.primary_photo || item?.image ? BADGES.photo : BADGES.noPhoto;
}

/** Locked versions whose pictures changed since the lock, across the Universe's characters. */
export function driftCount(characters) {
  return (characters ?? []).reduce(
    (n, c) => n + (c.versions ?? []).filter((v) => v.locked && (v.problems ?? []).length > 0).length, 0);
}

/**
 * The health line: "3 of 4 characters locked", plus the looks that changed since their lock when there
 * are any. counts: { characters, locked_characters } (GET /api/worlds counts).
 */
export function healthLine(counts, drift = 0) {
  const total = counts?.characters ?? 0;
  if (total === 0) return 'No characters yet';
  const locked = counts?.locked_characters ?? 0;
  const line = `${locked} of ${total} character${total === 1 ? '' : 's'} locked`;
  return drift > 0 ? `${line} · ${drift} look${drift === 1 ? '' : 's'} changed since lock` : line;
}

function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

/** "4 characters · 2 places · 1 prop · 5 episodes" (counts of GET /api/worlds). */
export function countsLine(counts) {
  return [
    plural(counts?.characters ?? 0, 'character'),
    plural(counts?.places ?? 0, 'place'),
    plural(counts?.props ?? 0, 'prop'),
    plural(counts?.episodes ?? 0, 'episode'),
  ].join(' · ');
}

/** "Used in 3 episodes"; usedIn: a number or { episodes }. */
export function usedInLabel(usedIn) {
  const n = typeof usedIn === 'number' ? usedIn : usedIn?.episodes ?? 0;
  return n === 0 ? 'Not used in an episode yet' : `Used in ${plural(n, 'episode')}`;
}

/** A name as names are compared: trimmed and lower case. */
export function sameName(name) {
  return String(name ?? '').trim().toLowerCase();
}

/**
 * The Universe character each drafted character is, by name (case-insensitive, trimmed):
 * { [code]: slug }. A name two Universe characters share, and a Universe character an earlier drafted
 * character already took, are left for the creator to pick.
 */
export function autoMapCast(rows, characters) {
  const byName = new Map();
  for (const c of characters ?? []) {
    const key = sameName(c.name);
    if (!key) continue;
    byName.set(key, byName.has(key) ? null : c.slug);
  }
  const taken = new Set();
  const out = {};
  for (const row of rows ?? []) {
    const slug = byName.get(sameName(row.name));
    if (!slug || taken.has(slug)) continue;
    taken.add(slug);
    out[row.code] = slug;
  }
  return out;
}

/**
 * What the wizard says about a drafted character in a Universe: "Same as in the Universe: Mara ·
 * Locked v2" for a Universe character, with the badge of the look a new episode uses (its default
 * version: the first locked one), else "New to this Universe".
 */
export function castMatchText(character) {
  if (!character) return { matched: false, text: 'New to this Universe', badge: null };
  const versions = character.versions ?? [];
  const used = versions.find((v) => v.version === character.default_version) ?? null;
  const badge = characterBadge(used, newestLocked(versions)?.version ?? null);
  return { matched: true, text: `Same as in the Universe: ${character.name} · ${badge.label}`, badge };
}
