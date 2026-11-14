/**
 * Cast helpers for the Cast panel (web/components/CastPanel.tsx) and the create wizard
 * (web/app/studio/create/page.tsx): the checks a picture gets before it is sent (the API checks it
 * again and decodes it: api/cast_routes.py), the rights answers every upload needs, where a
 * character sheet is and what comes next, who is in each shot, and the drafted cast the wizard adds
 * to a new episode.
 */
import { formatUsdMicros } from './balance.mjs';

export const PICTURE_TYPES = ['image/png', 'image/jpeg', 'image/webp'];
export const PICTURE_ACCEPT = '.png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp';
const PICTURE_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.webp'];
/** shots.json allows at most this many characters in one shot (hfvg/cast_prompt.py MAX_CAST_PER_SHOT). */
export const MAX_CAST_PER_SHOT = 6;
const ACTIVE_JOB = ['queued', 'running'];

export const PICTURE_LABELS = {
  upload: 'Your picture',
  master_option: 'Look option',
  master: 'Main look',
  turnaround_front: 'Front',
  turnaround_three_quarter: 'Three-quarter',
  turnaround_side: 'Side',
  turnaround_back: 'Back',
  head_sheet: 'Head close-ups',
};
/** The locked sheet, in the order the panel shows it. */
export const SHEET_ORDER = ['master', 'turnaround_front', 'turnaround_three_quarter', 'turnaround_side',
  'turnaround_back', 'head_sheet'];

const JOB_WORDS = {
  queued: 'waiting to start',
  running: 'in progress',
  completed: 'done',
  failed: 'did not work',
  refused: 'not started',
  skipped: 'skipped',
  pending_reconcile: 'checking the charge with the image service',
};

function megabytes(bytes) {
  const mb = Math.round((bytes / 1_048_576) * 10) / 10;
  return `${mb} MB`;
}

/** What is wrong with a chosen picture before it is sent (null when nothing is): its type and size. */
export function pictureProblem(file, maxBytes) {
  if (!file) return 'Choose a picture first.';
  const name = String(file.name ?? '').toLowerCase();
  const typeOk = file.type
    ? PICTURE_TYPES.includes(file.type)
    : PICTURE_EXTENSIONS.some(ext => name.endsWith(ext));
  if (!typeOk) return 'Pictures must be PNG, JPG or WebP files.';
  if (!file.size) return 'This file is empty.';
  if (maxBytes && file.size > maxBytes) {
    return `This picture is ${megabytes(file.size)}; pictures can be at most ${megabytes(maxBytes)}.`;
  }
  return null;
}

export function emptyRights() {
  return { rights: '', realPerson: false, likenessConsent: false };
}

/** What is missing from the rights answers (null when nothing is). */
export function rightsProblem(answers) {
  if (!answers || !['original', 'owned'].includes(answers.rights)) {
    return 'Tell us whose picture this is: you made it, or you own the rights to use it.';
  }
  if (answers.realPerson && !answers.likenessConsent) {
    return 'A picture of a real person needs their permission. Tick the consent box, or use an original character instead.';
  }
  return null;
}

/** The upload form fields of the rights answers (api/cast_routes.py upload_cast_image). */
export function rightsFields(answers) {
  return [
    ['rights', answers.rights],
    ['depicts_real_person', answers.realPerson ? 'true' : 'false'],
    ['likeness_consent', answers.realPerson && answers.likenessConsent ? 'true' : 'false'],
  ];
}

/** A version's pictures: the creator's uploads (main one first), the look options, and the sheet. */
export function versionPictures(version) {
  const images = (version?.images ?? []).filter(i => i.status === 'active');
  return {
    uploads: images.filter(i => i.role === 'upload').sort((a, b) => Number(b.is_primary) - Number(a.is_primary)),
    options: images.filter(i => i.role === 'master_option'),
    sheet: SHEET_ORDER.map(role => images.find(i => i.role === role)).filter(Boolean),
  };
}

export function activeJobs(version) {
  return (version?.sheet_jobs ?? []).filter(j => ACTIVE_JOB.includes(j.status));
}

/** True while any sheet picture of any character is being made (the panel checks back meanwhile). */
export function anyActiveJobs(characters) {
  return (characters ?? []).some(c => (c.versions ?? []).some(v => activeJobs(v).length > 0));
}

/** The jobs of the version's latest sheet request (one "Make them" press). */
export function latestRequest(version) {
  const jobs = version?.sheet_jobs ?? [];
  if (!jobs.length) return [];
  const last = jobs[jobs.length - 1].workflow_id;
  return jobs.filter(j => j.workflow_id === last);
}

export function jobStatusText(status) {
  return JOB_WORDS[status] ?? status;
}

/** Plain-words lines for the pictures of the latest request that were not made. */
export function requestProblems(version) {
  return latestRequest(version)
    .filter(j => ['failed', 'refused', 'pending_reconcile'].includes(j.status))
    .map(j => {
      const what = PICTURE_LABELS[j.view] ?? j.view;
      return `${what}: ${jobStatusText(j.status)}${j.error ? ` (${j.error})` : ''}`;
    });
}

/**
 * Where the version's character sheet is and what the creator does next:
 *   start -> (options made) pick -> (main look picked) turnaround -> (turnaround made) approve -> locked.
 * "working" while pictures are being made.
 */
export function sheetStep(version) {
  if (!version) return 'start';
  if (version.locked) return 'locked';
  if (activeJobs(version).length) return 'working';
  switch (version.sheet_stage) {
    case 'options_ready': return 'pick';
    case 'master_picked': return 'turnaround';
    case 'turnaround_ready': return 'approve';
    default: return 'start';
  }
}

export const SHEET_STEP_TEXT = {
  start: 'Make a few looks to choose from. Pick one, and we turn it into a full character sheet.',
  working: 'Making the pictures. This usually takes a minute or two; you can leave this page.',
  pick: 'Pick the look that fits best. The rest of the sheet is made from it.',
  turnaround: 'Now make the turnaround: front, three-quarter, side and back, plus head close-ups.',
  approve: 'Check the sheet. When you approve it, this look is locked and used for every shot with this character.',
  locked: 'Locked. These pictures keep this character looking the same in every shot.',
};

/** Look options asked for from the description (2-4) or from a picture (1-4); defaults 3 and 2. */
export function optionCounts(fromPicture) {
  return fromPicture ? [1, 2, 3, 4] : [2, 3, 4];
}

export function defaultOptionCount(fromPicture) {
  return fromPicture ? 2 : 3;
}

/** "3 pictures, about $1.17, charged to this video's budget" (or free stand-ins in preview mode). */
export function estimateText(estimate) {
  const n = estimate.count;
  const pictures = `${n} picture${n === 1 ? '' : 's'}`;
  if (estimate.dry_run) return `${pictures}, free: preview mode makes simple stand-in pictures`;
  return `${pictures}, about ${formatUsdMicros(estimate.total_usd_micros)}, charged to this video's budget`;
}

/** The sheet still needs placeholder approval (dry-run stand-ins) to be locked. */
export function sheetHasPlaceholders(version) {
  return versionPictures(version).sheet.some(i => i.placeholder);
}

// ---------------------------------------------------------------------------------- who is in each shot

/** A shot's cast as picks [{character, version}] (version null = the character's default version). */
export function castPicks(cast) {
  return (cast ?? []).map(entry => (typeof entry === 'string'
    ? { character: entry, version: null }
    : { character: entry.character, version: entry.version ?? null }));
}

/** shots.json `cast` entries of picks: the id alone for the default version. */
export function castEntries(picks) {
  return picks.map(p => (p.version ? { character: p.character, version: p.version } : p.character));
}

/** Add or remove a character from a shot's picks (order kept; at most MAX_CAST_PER_SHOT). */
export function toggleCast(picks, slug) {
  if (picks.some(p => p.character === slug)) return picks.filter(p => p.character !== slug);
  if (picks.length >= MAX_CAST_PER_SHOT) return picks;
  return [...picks, { character: slug, version: null }];
}

export function setCastVersion(picks, slug, version) {
  return picks.map(p => (p.character === slug ? { ...p, version: version || null } : p));
}

export function samePicks(a, b) {
  return JSON.stringify(castEntries(a ?? [])) === JSON.stringify(castEntries(b ?? []));
}

/**
 * POST /api/episodes/{id}/cast/shots body for the shots whose picks or words setting changed.
 * Returns null when nothing changed.
 */
export function shotCastChanges(shots, picks, allowText) {
  const body = { shots: {}, allow_text: {} };
  for (const shot of shots) {
    const id = shot.shot_id;
    if (picks[id] && !samePicks(castPicks(shot.cast), picks[id])) body.shots[id] = castEntries(picks[id]);
    const saved = shot.allow_text ?? null;
    if (id in allowText && (allowText[id] ?? null) !== saved) body.allow_text[id] = allowText[id] ?? null;
  }
  return Object.keys(body.shots).length || Object.keys(body.allow_text).length ? body : null;
}

// ---------------------------------------------------------------------------------- create wizard

/** The wizard's editable rows of draft-from-idea's cast. `seriesSlug` maps a row to a series character. */
export function draftCastRows(cast) {
  return (cast ?? []).map(c => ({
    code: String(c.code),
    slug: c.slug,
    name: c.name ?? '',
    role: c.role ?? '',
    description: c.description ?? '',
    shots: c.shots ?? [],
    seriesSlug: '',
  }));
}

/** What stops the wizard from adding the drafted cast (null when nothing does). */
export function castRowsProblem(rows) {
  const unnamed = rows.find(r => !r.seriesSlug && !r.name.trim());
  return unnamed ? `Give character ${unnamed.code} a name, or pick one from the series.` : null;
}

/** POST /api/episodes/{id}/cast/import body; a row mapped to a series character reuses it. */
export function castImportBody(rows, seriesCharacters = []) {
  return {
    characters: rows.map(r => {
      const same = r.seriesSlug ? seriesCharacters.find(c => c.slug === r.seriesSlug) : null;
      if (same) return { code: r.code, slug: same.slug, name: same.name, role: same.role ?? '', description: same.description ?? '' };
      return { code: r.code, slug: r.slug, name: r.name.trim(), role: r.role.trim(), description: r.description.trim() };
    }),
  };
}

/** shots.json with each shot's `cast`: the ids the import gave the drafted characters in that shot. */
export function shotsWithCast(shotsJson, rows, imported) {
  const slugByCode = Object.fromEntries((imported ?? []).map(c => [String(c.code), c.slug]));
  const shots = shotsJson.shots.map(shot => {
    const slugs = rows.filter(r => r.shots.includes(shot.id)).map(r => slugByCode[r.code]).filter(Boolean);
    const cast = [...new Set(slugs)].slice(0, MAX_CAST_PER_SHOT);
    return cast.length ? { ...shot, cast } : shot;
  });
  return { ...shotsJson, shots };
}

/** prompt_kit.json with the creator's names and looks (a series character keeps the series' text). */
export function kitWithCast(kit, rows, seriesCharacters = []) {
  const characters = { ...(kit?.characters ?? {}) };
  for (const r of rows) {
    if (!(r.code in characters)) continue;
    const before = characters[r.code];
    const entry = before && typeof before === 'object' ? before : { description: String(before ?? '') };
    const same = r.seriesSlug ? seriesCharacters.find(c => c.slug === r.seriesSlug) : null;
    const name = same ? same.name : r.name.trim();
    const description = same ? same.description : r.description.trim();
    const role = same ? same.role : r.role.trim();
    characters[r.code] = {
      ...entry,
      ...(name ? { name } : {}),
      ...(description ? { description } : {}),
      ...(role ? { role } : {}),
    };
  }
  return { ...kit, characters };
}
