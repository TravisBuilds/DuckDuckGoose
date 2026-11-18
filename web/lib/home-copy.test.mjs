import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { BANNED_WORDS, ICON_CHARACTERS, copyProblems } from './copy-rules.mjs';
import { HOME_COPY, copyStrings } from './home-copy.mjs';
import { HOME_PICTURES, PICTURE_FORMATS, pictureUrl } from './home-pictures.mjs';
import { literalsIn, webDir } from './source-text.mjs';

/** The home page's source: its route and everything under components/home/. */
const HOME_SOURCES = [
  'app/page.tsx',
  ...readdirSync(join(webDir, 'components/home'), { recursive: true })
    .filter((file) => /\.tsx?$/.test(file))
    .map((file) => join('components/home', file)),
];

const problemsIn = (strings) =>
  strings.flatMap(({ path, text }) => copyProblems(text).map(({ rule, match }) => `${path}: ${rule} ("${match}")`));

test('the copy rules catch every banned word, in any case and in its other forms', () => {
  const forms = {
    creators: ['creators', 'Creator', 'CREATORS'],
    IP: ['IP', 'your ip', 'I.P.'],
    harness: ['harness', 'Harnessing', 'harnessed'],
    'co-pilot': ['co-pilot', 'Copilot', 'co pilot', 'co\u2011pilots'],
    content: ['content', 'Contents'],
    unlock: ['unlock', 'Unlocks', 'unlocking', 'unlocked'],
    leverage: ['leverage', 'Leveraging', 'leveraged'],
    platform: ['platform', 'Platforms'],
    synergy: ['synergy', 'synergies', 'Synergistic'],
    'AI-powered': ['AI-powered', 'ai powered', 'AI\u2011Powered'],
    revolutionize: ['revolutionize', 'Revolutionise', 'revolutionizing', 'revolutionary'],
  };
  assert.deepEqual(Object.keys(forms).sort(), BANNED_WORDS.map(({ word }) => word).sort());
  for (const [word, samples] of Object.entries(forms)) {
    for (const sample of samples) {
      const rules = copyProblems(`We ${sample} things.`).map(({ rule }) => rule);
      assert.ok(rules.includes(`banned word "${word}"`), `"${sample}" should break the "${word}" rule`);
    }
  }
});

test('the copy rules catch pricing, investor, founder and contact-us copy', () => {
  const samples = [
    ['pricing', 'See our pricing'],
    ['pricing', 'Only $9, cancel any time'],
    ['investors', 'Loved by investors'],
    ['founders', 'Meet the founders'],
    ['contact us', 'Contact us today'],
    ['contact us', 'Get in touch'],
  ];
  for (const [topic, text] of samples) {
    assert.ok(copyProblems(text).some(({ rule }) => rule === `no ${topic} section`), `"${text}" should break the ${topic} rule`);
  }
});

test('the copy rules leave ordinary words alone', () => {
  for (const text of ['A tip: ship it, zip it, equip it.', 'Pure contentment.', 'Your life, back', 'AI that does the work']) {
    assert.deepEqual(copyProblems(text), [], text);
  }
});

test('every word on the home page follows the copy rules', () => {
  const strings = copyStrings(HOME_COPY);
  assert.ok(strings.length > 40, `only ${strings.length} strings found in HOME_COPY`);
  assert.deepEqual(problemsIn(strings), []);
});

test('the home page has one call to action, worded the same everywhere', () => {
  const labels = new Set([HOME_COPY.header.cta, HOME_COPY.hero.cta, HOME_COPY.vision.cta]);
  assert.deepEqual([...labels], ['Get early access']);
});

test('the home components write no text of their own: all of it comes from lib/home-copy.mjs', () => {
  assert.ok(HOME_SOURCES.length >= 8, `found only ${HOME_SOURCES.join(', ')}`);
  const written = HOME_SOURCES.flatMap(literalsIn).filter(({ visible }) => visible);
  assert.deepEqual(written.map(({ path, text }) => `${path}: "${text}"`), []);
});

test('no string in the home components breaks the copy rules either', () => {
  assert.deepEqual(problemsIn(HOME_SOURCES.flatMap(literalsIn)), []);
});

test('the home page draws no icons: no icon sets or icon components, no symbol characters, svg only for the deck shapes', () => {
  for (const { path, text } of copyStrings(HOME_COPY)) assert.doesNotMatch(text, ICON_CHARACTERS, path);
  for (const file of HOME_SOURCES) {
    const source = readFileSync(join(webDir, file), 'utf8');
    assert.doesNotMatch(source, ICON_CHARACTERS, `${file} has an emoji or symbol character`);
    assert.doesNotMatch(source, /from ['"](?:@\/components\/playful|lucide-react|react-icons|@heroicons)/, `${file} imports an icon set`);
    assert.doesNotMatch(source, /<(?:Icon|IconCircle|LogoMark|Avatar|Seal)\b/, `${file} uses an icon component`);
    if (!file.endsWith('parts.tsx')) assert.doesNotMatch(source, /<svg\b/, `${file} draws an svg: only parts.tsx's deck shapes may`);
  }
});

test('the home page has exactly one h1', () => {
  const h1s = HOME_SOURCES.flatMap((file) => readFileSync(join(webDir, file), 'utf8').match(/<h1\b/g) ?? []);
  assert.equal(h1s.length, 1);
});

test('every home picture is there as AVIF and WebP at each width, and small', () => {
  const MAGIC = { avif: (bytes) => bytes.toString('latin1', 4, 12) === 'ftypavif', webp: (bytes) => bytes.toString('latin1', 8, 12) === 'WEBP' };
  for (const [name, picture] of Object.entries(HOME_PICTURES)) {
    assert.ok(picture.widths.every((width) => width <= picture.width), `${name} is never enlarged`);
    for (const width of picture.widths) {
      for (const format of PICTURE_FORMATS) {
        const file = join(webDir, 'public', pictureUrl(name, width, format));
        assert.ok(existsSync(file), `${file} is missing: run scripts/home-pictures.mjs`);
        const { size } = statSync(file);
        assert.ok(size > 1024 && size < 120 * 1024, `${file} is ${size} bytes`);
        assert.ok(MAGIC[format](readFileSync(file).subarray(0, 12)), `${file} is not ${format}`);
      }
    }
  }
});

test('every home picture has alt text that describes it', () => {
  const alts = {
    ...Object.fromEntries(HOME_COPY.stories.items.map(({ key, alt }) => [key, alt])),
    home: HOME_COPY.video.alt,
    stage: HOME_COPY.how.alt,
    meadow: HOME_COPY.vision.alt,
  };
  assert.deepEqual(Object.keys(alts).sort(), Object.keys(HOME_PICTURES).sort());
  for (const [name, alt] of Object.entries(alts)) {
    assert.ok(alt.split(/\s+/).length >= 6, `${name}: "${alt}" is too short to describe the photo`);
    assert.doesNotMatch(alt, /^(?:an? )?(?:image|picture|photo)\b/i, `${name}: alt text describes the photo, not that it is one`);
  }
});
