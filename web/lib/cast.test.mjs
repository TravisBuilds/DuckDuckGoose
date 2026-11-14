import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  MAX_CAST_PER_SHOT,
  SHEET_STEP_TEXT,
  anyActiveJobs,
  castEntries,
  castImportBody,
  castPicks,
  castRowsProblem,
  defaultOptionCount,
  draftCastRows,
  emptyRights,
  estimateText,
  kitWithCast,
  latestRequest,
  optionCounts,
  pictureProblem,
  requestProblems,
  rightsFields,
  rightsProblem,
  samePicks,
  setCastVersion,
  sheetHasPlaceholders,
  sheetStep,
  shotCastChanges,
  shotsWithCast,
  toggleCast,
  versionPictures,
} from './cast.mjs';

const MB = 1_048_576;

function image(role, extra = {}) {
  return { image_id: `ci_${role}`, role, status: 'active', is_primary: false, placeholder: false, ...extra };
}

function job(view, status, workflow, extra = {}) {
  return { job_id: `${workflow}-${view}`, view, status, workflow_id: workflow, error: null, ...extra };
}

test('a picture must be PNG, JPG or WebP, not empty and under the size limit', () => {
  assert.equal(pictureProblem(null, 15 * MB), 'Choose a picture first.');
  assert.equal(pictureProblem({ name: 'pip.png', type: 'image/png', size: 2 * MB }, 15 * MB), null);
  assert.equal(pictureProblem({ name: 'pip.jpg', type: 'image/jpeg', size: 1 }, 15 * MB), null);
  assert.equal(pictureProblem({ name: 'pip.webp', type: 'image/webp', size: 1 }, 15 * MB), null);
  // a browser that reports no type: the extension decides
  assert.equal(pictureProblem({ name: 'PIP.JPEG', type: '', size: 1 }, 15 * MB), null);
  assert.equal(pictureProblem({ name: 'pip.gif', type: 'image/gif', size: 1 }, 15 * MB), 'Pictures must be PNG, JPG or WebP files.');
  assert.equal(pictureProblem({ name: 'pip.png', type: 'image/gif', size: 1 }, 15 * MB), 'Pictures must be PNG, JPG or WebP files.');
  assert.equal(pictureProblem({ name: 'notes.txt', type: '', size: 1 }, 15 * MB), 'Pictures must be PNG, JPG or WebP files.');
  assert.equal(pictureProblem({ name: 'pip.png', type: 'image/png', size: 0 }, 15 * MB), 'This file is empty.');
  assert.equal(pictureProblem({ name: 'big.png', type: 'image/png', size: 20.5 * MB }, 15 * MB),
    'This picture is 20.5 MB; pictures can be at most 15 MB.');
  assert.equal(pictureProblem({ name: 'big.png', type: 'image/png', size: 20 * MB }, null), null);
});

test('every upload needs the rights answer, and a real person needs their consent', () => {
  assert.match(rightsProblem(emptyRights()), /whose picture this is/);
  assert.match(rightsProblem(null), /whose picture this is/);
  assert.equal(rightsProblem({ rights: 'original', realPerson: false, likenessConsent: false }), null);
  assert.equal(rightsProblem({ rights: 'owned', realPerson: false, likenessConsent: false }), null);
  assert.match(rightsProblem({ rights: 'owned', realPerson: true, likenessConsent: false }), /real person needs their permission/);
  assert.equal(rightsProblem({ rights: 'owned', realPerson: true, likenessConsent: true }), null);
  assert.match(rightsProblem({ rights: 'mine', realPerson: false, likenessConsent: false }), /whose picture/);
});

test('the rights answers become the upload form fields; consent only counts for a real person', () => {
  assert.deepEqual(rightsFields({ rights: 'original', realPerson: false, likenessConsent: true }), [
    ['rights', 'original'], ['depicts_real_person', 'false'], ['likeness_consent', 'false'],
  ]);
  assert.deepEqual(rightsFields({ rights: 'owned', realPerson: true, likenessConsent: true }), [
    ['rights', 'owned'], ['depicts_real_person', 'true'], ['likeness_consent', 'true'],
  ]);
});

test('pictures are grouped: uploads with the main one first, look options, the sheet in order', () => {
  const version = {
    images: [
      image('head_sheet'), image('upload', { image_id: 'ci_a' }), image('turnaround_side'),
      image('upload', { image_id: 'ci_b', is_primary: true }), image('master_option'), image('master'),
      image('master_option', { status: 'discarded', image_id: 'ci_old' }), image('turnaround_front'),
    ],
  };
  const pictures = versionPictures(version);
  assert.deepEqual(pictures.uploads.map(i => i.image_id), ['ci_b', 'ci_a']);
  assert.deepEqual(pictures.options.map(i => i.image_id), ['ci_master_option']);
  assert.deepEqual(pictures.sheet.map(i => i.role), ['master', 'turnaround_front', 'turnaround_side', 'head_sheet']);
  assert.deepEqual(versionPictures(undefined), { uploads: [], options: [], sheet: [] });
});

test('the sheet step follows the version: start, working, pick, turnaround, approve, locked', () => {
  assert.equal(sheetStep(undefined), 'start');
  assert.equal(sheetStep({ sheet_stage: 'none', sheet_jobs: [] }), 'start');
  assert.equal(sheetStep({ sheet_stage: 'options_ready', sheet_jobs: [] }), 'pick');
  assert.equal(sheetStep({ sheet_stage: 'master_picked', sheet_jobs: [] }), 'turnaround');
  assert.equal(sheetStep({ sheet_stage: 'turnaround_ready', sheet_jobs: [] }), 'approve');
  assert.equal(sheetStep({ sheet_stage: 'master_picked', sheet_jobs: [job('head_sheet', 'running', 'w2')] }), 'working');
  assert.equal(sheetStep({ sheet_stage: 'none', sheet_jobs: [job('master_option', 'queued', 'w1')] }), 'working');
  assert.equal(sheetStep({ locked: true, sheet_stage: 'turnaround_ready', sheet_jobs: [] }), 'locked');
  for (const step of ['start', 'working', 'pick', 'turnaround', 'approve', 'locked']) {
    assert.ok(SHEET_STEP_TEXT[step].length > 20, step);
  }
});

test('only the latest request reports its problems, in plain words', () => {
  const version = {
    sheet_jobs: [
      job('master_option', 'failed', 'w1', { error: 'old problem' }),
      job('turnaround_front', 'completed', 'w2'),
      job('turnaround_side', 'failed', 'w2', { error: 'The image service did not answer' }),
      job('turnaround_back', 'refused', 'w2', { error: 'Balance check: too low' }),
      job('head_sheet', 'pending_reconcile', 'w2'),
    ],
  };
  assert.deepEqual(latestRequest(version).map(j => j.view),
    ['turnaround_front', 'turnaround_side', 'turnaround_back', 'head_sheet']);
  assert.deepEqual(requestProblems(version), [
    'Side: did not work (The image service did not answer)',
    'Back: not started (Balance check: too low)',
    'Head close-ups: checking the charge with the image service',
  ]);
  assert.deepEqual(requestProblems({ sheet_jobs: [] }), []);
  assert.equal(anyActiveJobs([{ versions: [{ sheet_jobs: [job('master', 'completed', 'w')] }] }]), false);
  assert.equal(anyActiveJobs([{ versions: [{ sheet_jobs: [] }, { sheet_jobs: [job('master', 'running', 'w')] }] }]), true);
  assert.equal(anyActiveJobs(undefined), false);
});

test('look options: 2-4 from the description, 1-4 from a picture', () => {
  assert.deepEqual(optionCounts(false), [2, 3, 4]);
  assert.deepEqual(optionCounts(true), [1, 2, 3, 4]);
  assert.equal(defaultOptionCount(false), 3);
  assert.equal(defaultOptionCount(true), 2);
});

test('the estimate is shown before anything is made, free in preview mode', () => {
  assert.equal(estimateText({ count: 3, dry_run: false, total_usd_micros: 1_170_000 }),
    "3 pictures, about $1.17, charged to this video's budget");
  assert.equal(estimateText({ count: 1, dry_run: false, total_usd_micros: 390_000 }),
    "1 picture, about $0.39, charged to this video's budget");
  assert.equal(estimateText({ count: 5, dry_run: true, total_usd_micros: 0 }),
    '5 pictures, free: preview mode makes simple stand-in pictures');
});

test('a sheet of stand-in pictures is flagged', () => {
  assert.equal(sheetHasPlaceholders({ images: [image('master', { placeholder: true })] }), true);
  assert.equal(sheetHasPlaceholders({ images: [image('master'), image('upload', { placeholder: true })] }), false);
});

test('shot cast picks round-trip to shots.json entries; the default version is the id alone', () => {
  const picks = castPicks(['pip', { character: 'wren', version: 2 }, { character: 'moss' }]);
  assert.deepEqual(picks, [
    { character: 'pip', version: null }, { character: 'wren', version: 2 }, { character: 'moss', version: null },
  ]);
  assert.deepEqual(castEntries(picks), ['pip', { character: 'wren', version: 2 }, 'moss']);
  assert.deepEqual(castPicks(null), []);
});

test('toggling a character adds it at the end or removes it, up to the per-shot limit', () => {
  let picks = toggleCast([], 'pip');
  picks = toggleCast(picks, 'wren');
  assert.deepEqual(castEntries(picks), ['pip', 'wren']);
  assert.deepEqual(castEntries(toggleCast(picks, 'pip')), ['wren']);
  let full = [];
  for (let i = 0; i < MAX_CAST_PER_SHOT; i++) full = toggleCast(full, `c${i}`);
  assert.equal(toggleCast(full, 'one-more'), full);
  assert.deepEqual(castEntries(setCastVersion(picks, 'wren', 3)), ['pip', { character: 'wren', version: 3 }]);
  assert.deepEqual(castEntries(setCastVersion(picks, 'wren', null)), ['pip', 'wren']);
});

test('only changed shots are sent, and nothing when nothing changed', () => {
  const shots = [
    { shot_id: 'S01', cast: [{ character: 'pip', version: null }], allow_text: null },
    { shot_id: 'S02', cast: [], allow_text: true },
    { shot_id: 'S03', cast: [], allow_text: null },
  ];
  const picks = {
    S01: [{ character: 'pip', version: null }],
    S02: [{ character: 'wren', version: 2 }, { character: 'pip', version: null }],
    S03: [],
  };
  assert.deepEqual(shotCastChanges(shots, picks, { S02: true, S03: true }), {
    shots: { S02: [{ character: 'wren', version: 2 }, 'pip'] },
    allow_text: { S03: true },
  });
  assert.deepEqual(shotCastChanges(shots, { S01: [] }, { S02: null }), {
    shots: { S01: [] },
    allow_text: { S02: null },
  });
  assert.equal(shotCastChanges(shots, { S01: castPicks(['pip']) }, { S02: true }), null);
  assert.equal(samePicks(undefined, []), true);
});

const DRAFT_CAST = [
  { code: 'A', slug: 'mara', name: 'Mara', role: 'lead', description: 'A tall courier in a yellow raincoat', shots: ['S01', 'S02'] },
  { code: 'B', slug: 'tobin', name: 'Tobin', role: 'friend', description: 'A short baker with flour on his apron', shots: ['S02', 'S03'] },
];
const SERIES = [{ slug: 'pip', name: 'Pip', role: 'lead', description: 'A small fox courier with a blue scarf' }];

test('the drafted cast becomes editable rows; a row needs a name unless it uses a series character', () => {
  const rows = draftCastRows(DRAFT_CAST);
  assert.deepEqual(rows.map(r => [r.code, r.slug, r.name, r.seriesSlug]), [['A', 'mara', 'Mara', ''], ['B', 'tobin', 'Tobin', '']]);
  assert.equal(castRowsProblem(rows), null);
  rows[1].name = '  ';
  assert.equal(castRowsProblem(rows), 'Give character B a name, or pick one from the series.');
  rows[1].seriesSlug = 'pip';
  assert.equal(castRowsProblem(rows), null);
  assert.deepEqual(draftCastRows(undefined), []);
});

test('the import body carries the edited text; a series character is reused by its id', () => {
  const rows = draftCastRows(DRAFT_CAST);
  rows[0].name = ' Mara Vell ';
  rows[0].description = ' A tall courier in a yellow raincoat and green boots ';
  rows[1].seriesSlug = 'pip';
  assert.deepEqual(castImportBody(rows, SERIES), {
    characters: [
      { code: 'A', slug: 'mara', name: 'Mara Vell', role: 'lead', description: 'A tall courier in a yellow raincoat and green boots' },
      { code: 'B', slug: 'pip', name: 'Pip', role: 'lead', description: 'A small fox courier with a blue scarf' },
    ],
  });
});

test('each drafted shot gets the cast of the characters in it; other shots stay as drafted', () => {
  const rows = draftCastRows(DRAFT_CAST);
  const shotsJson = { shots: [{ id: 'S01', prompt: 'a' }, { id: 'S02', prompt: 'b' }, { id: 'S03', prompt: 'c' }, { id: 'S04', prompt: 'd' }] };
  const out = shotsWithCast(shotsJson, rows, [{ code: 'A', slug: 'mara' }, { code: 'B', slug: 'pip' }]);
  assert.deepEqual(out.shots.map(s => s.cast ?? null), [['mara'], ['mara', 'pip'], ['pip'], null]);
  assert.deepEqual(out.shots[3], { id: 'S04', prompt: 'd' });
  assert.equal(shotsJson.shots[0].cast, undefined, 'the draft itself is not changed');
  // two drafted characters mapped to the same series character are listed once
  const same = shotsWithCast(shotsJson, rows, [{ code: 'A', slug: 'pip' }, { code: 'B', slug: 'pip' }]);
  assert.deepEqual(same.shots[1].cast, ['pip']);
});

test('the prompt kit gets the edited names and looks; a series character keeps the series text', () => {
  const kit = {
    style: 'soft watercolor',
    characters: { A: { name: 'Mara', description: 'old text', voice: 'x' }, B: 'a bare description', C: { name: 'Extra' } },
  };
  const rows = draftCastRows(DRAFT_CAST);
  rows[0].description = 'A tall courier in a yellow raincoat and green boots';
  rows[1].seriesSlug = 'pip';
  const out = kitWithCast(kit, rows, SERIES);
  assert.deepEqual(out.characters.A, {
    name: 'Mara', description: 'A tall courier in a yellow raincoat and green boots', voice: 'x', role: 'lead',
  });
  assert.deepEqual(out.characters.B, { name: 'Pip', description: 'A small fox courier with a blue scarf', role: 'lead' });
  assert.deepEqual(out.characters.C, { name: 'Extra' });
  assert.equal(out.style, 'soft watercolor');
  assert.equal(kit.characters.A.description, 'old text', 'the draft kit itself is not changed');
});
