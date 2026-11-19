import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import ts from 'typescript';
import { ICON_CHARACTERS, MODEL_NAMES, bannedWords } from './copy-rules.mjs';
import { ledgerWords } from './checkout.mjs';
import { HOME_COPY } from './home-copy.mjs';
import { importsOf, literalsIn, sourcesFrom, tagsIn, webDir } from './source-text.mjs';

/**
 * Sign-in, the password page, the studio and the cost dashboard keep to the owner's rules: no icons
 * of any kind (type, colour and shape only) and none of the banned words. The rules cover every file
 * those routes are built from, followed through their imports, and the root layout with the shared
 * header and footer. The home page has its own tests (lib/home-copy.test.mjs).
 */

const routeFiles = (dir) => readdirSync(join(webDir, dir), { recursive: true })
  .filter((file) => /(?:^|\/)(?:page|layout)\.tsx$/.test(file))
  .map((file) => join(dir, file));

const ENTRIES = ['app/layout.tsx', 'app/login/page.tsx', 'app/set-password/page.tsx', ...routeFiles('app/studio'), ...routeFiles('app/dashboard')];
const SOURCES = sourcesFrom(ENTRIES).filter((file) => !file.startsWith('components/home/'));
const LITERALS = SOURCES.flatMap(literalsIn);

/** Icon sets from npm, and the studio's own line icons and play-triangle logo, removed for good. */
const ICON_PACKAGES = /^(?:lucide-react|react-icons|@heroicons\/|@radix-ui\/react-icons|@fortawesome\/|@mui\/icons-material|@phosphor-icons\/|@tabler\/icons|react-feather|@iconify\/|ionicons)/;
const ICON_FILES = /(?:^|\/)(?:Icon|IconCircle|LogoMark)(?:\.tsx?)?$/;

/**
 * Punctuation that may stand alone between words: separators, brackets, quotes and the "—" of a missing
 * value. A dash or "×" between two values is a range or a multiplication. Any other lone symbol ("+",
 * "×", "!", "•", "?", or a "$" badge on a step) would be an icon in disguise.
 */
const JOINERS = /^[\s·/:,.()…;“”"'—]*$/u;
const BETWEEN_VALUES = /^[–×]$/u;

test('the rules cover the sign-in, password, studio and dashboard routes and everything they are built from', () => {
  for (const file of ['app/login/page.tsx', 'app/set-password/page.tsx', 'app/studio/page.tsx', 'app/dashboard/page.tsx',
    'components/StudioShell.tsx', 'components/playful/BottomBar.tsx', 'components/playful/Wordmark.tsx',
    'components/Header.tsx', 'components/Footer.tsx', 'components/Toast.tsx', 'components/UniverseCreation.tsx',
    // the story-first screen, its reference list and the small-picture notes
    'components/StoryChecklist.tsx', 'lib/checklist.mjs', 'components/PictureSize.tsx', 'lib/cast.mjs',
    // the first frames of an episode's shots, and the start that waits for them
    'components/ConceptFrames.tsx', 'lib/frames.mjs', 'components/UniverseEpisode.tsx', 'app/studio/create/page.tsx',
    // every place someone meets a price or an approval (docs/EASY-CHECKOUT.md)
    'components/Checkout.tsx', 'lib/checkout.mjs', 'components/CastPanel.tsx', 'lib/world.mjs', 'components/NextStepCard.tsx',
    'lib/next-step.mjs', 'components/EpisodeRunPanel.tsx', 'lib/redo.mjs', 'lib/run-outcome.mjs', 'components/ShotPlanEditor.tsx',
    'app/studio/episodes/[id]/page.tsx', 'components/playful/JourneyPath.tsx', 'components/playful/CharacterCard.tsx']) {
    assert.ok(SOURCES.includes(file), `${file} is not covered`);
  }
  assert.ok(SOURCES.length > 60, `only ${SOURCES.length} files found`);
  assert.ok(LITERALS.filter(({ visible }) => visible).length > 1000, 'too few visible strings found');
});

test('the line icons and the play-triangle logo are gone, and nothing imports them or an icon set', () => {
  for (const name of ['Icon', 'IconCircle', 'LogoMark']) {
    assert.ok(!existsSync(join(webDir, 'components/playful', `${name}.tsx`)), `components/playful/${name}.tsx is back`);
  }
  for (const file of SOURCES) {
    for (const specifier of importsOf(file)) {
      assert.doesNotMatch(specifier, ICON_PACKAGES, `${file} imports the icon set ${specifier}`);
      assert.doesNotMatch(specifier, ICON_FILES, `${file} imports ${specifier}`);
    }
    const playful = readFileSync(join(webDir, file), 'utf8').match(/import\s*\{([^}]*)\}\s*from\s*['"](?:@\/components\/playful|\.\/playful|\.)['"]/);
    if (playful) assert.doesNotMatch(playful[1], /\b(?:Icon|IconCircle|LogoMark)\b/, `${file} imports an icon component`);
  }
});

test('these pages draw no icons: no svg, no icon pictures, no symbol characters', () => {
  const drawn = SOURCES.flatMap(tagsIn).filter(({ tag }) => /^(?:svg|path|use|symbol|Icon|IconCircle|LogoMark)$/.test(tag));
  assert.deepEqual(drawn.map(({ path, tag }) => `${path}: <${tag}>`), []);
  const symbols = LITERALS.filter(({ text }) => ICON_CHARACTERS.test(text));
  assert.deepEqual(symbols.map(({ path, text }) => `${path}: "${text}"`), []);
  const pictures = LITERALS.filter(({ text }) => /\.svg(?:$|\?)|\/icons?\//i.test(text));
  assert.deepEqual(pictures.map(({ path, text }) => `${path}: "${text}"`), []);
});

test('no lone symbol stands in for an icon: no "+" button, "×" close, "!" badge or "✓" mark', () => {
  const lone = LITERALS.filter(({ text, child, between }) => child && !/[\p{L}\p{N}]/u.test(text)
    && !JOINERS.test(text) && !(between && BETWEEN_VALUES.test(text.trim())));
  assert.deepEqual(lone.map(({ path, text }) => `${path}: "${text}"`), []);
});

test('the lone-symbol rule catches icon stand-ins and lets punctuation through', () => {
  const isLone = (text, between = false) => !/[\p{L}\p{N}]/u.test(text) && !JOINERS.test(text) && !(between && BETWEEN_VALUES.test(text.trim()));
  for (const glyph of ['+', '×', '!', '•', '?', '*', '–', '$']) assert.ok(isLone(glyph), `"${glyph}" alone should count as an icon`);
  for (const text of ['·', ' / ', '—', ':', '(', '…']) assert.ok(!isLone(text), `"${text}" is punctuation`);
  assert.ok(!isLone('$4.20') && !isLone('about $1.56'), 'a price is words, not a badge');
  assert.ok(!isLone('–', true) && !isLone('×', true), 'a range dash or a multiplication between two values is fine');
  for (const glyph of ['✓', '✗', '→', '←', '▶', '■', '★', '✦', '⚠', '\uE001', '🎬']) assert.match(glyph, ICON_CHARACTERS);
  for (const text of ['·', '—', '…', '©', '×', '$4.20', 'Pebble Cove']) assert.doesNotMatch(text, ICON_CHARACTERS);
});

/** The screens where someone meets a price or an approval (docs/EASY-CHECKOUT.md). */
const MONEY_SCREENS = ['components/Checkout.tsx', 'components/ConceptFrames.tsx', 'lib/frames.mjs', 'components/CastPanel.tsx',
  'lib/cast.mjs', 'components/UniverseCreation.tsx', 'lib/world.mjs', 'components/UniverseEpisode.tsx', 'components/NextStepCard.tsx',
  'lib/next-step.mjs', 'lib/redo.mjs', 'lib/run-outcome.mjs', 'components/EpisodeRunPanel.tsx', 'components/ShotPlanEditor.tsx',
  'app/studio/create/page.tsx', 'app/studio/episodes/[id]/page.tsx', 'components/playful/JourneyPath.tsx'];

/** Cards in an episode's collapsed Production details (two of them for admins only): they keep the full ledger. */
const DETAILS_CARDS = new Set(['LiveModeCard', 'BudgetCard', 'ModelLimitsCard', 'CreditsCard']);

/** The first and last line of each named function in a file. */
function functionLines(file, names) {
  const source = ts.createSourceFile(file, readFileSync(join(webDir, file), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const lines = [];
  const visit = (node) => {
    if (ts.isFunctionDeclaration(node) && node.name && names.has(node.name.text)) {
      lines.push({ name: node.name.text, from: source.getLineAndCharacterOfPosition(node.getStart()).line + 1,
        to: source.getLineAndCharacterOfPosition(node.getEnd()).line + 1 });
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return lines;
}

test('the money screens say one plain price: no ledger words, no model or provider names', () => {
  const problems = [];
  const cards = [];
  for (const file of MONEY_SCREENS) {
    assert.ok(SOURCES.includes(file), `${file} is not covered`);
    const details = functionLines(file, DETAILS_CARDS);
    cards.push(...details.map(({ name }) => name));
    for (const { path, text, visible } of literalsIn(file)) {
      const line = Number(path.slice(path.lastIndexOf(':') + 1));
      if (!visible || details.some(({ from, to }) => line >= from && line <= to)) continue;
      for (const word of ledgerWords(text)) problems.push(`${path}: "${word}" in "${text}"`);
      const name = text.match(MODEL_NAMES);
      if (name) problems.push(`${path}: "${name[0]}" in "${text}"`);
    }
  }
  assert.deepEqual(problems, []);
  assert.deepEqual(cards.sort(), [...DETAILS_CARDS].sort(), 'every Production details card is found, and only those');
});

test('the money rules catch ledger words and model names, and let plain prices through', () => {
  for (const text of ['Charged to the GC.01 budget line', 'about $1.20, minus the reserve', 'Uses real credits',
    'The estimate is now $4.20']) assert.ok(ledgerWords(text).length > 0, `"${text}" uses ledger words`);
  for (const text of ['made with kling-2.6', 'Grok Imagine', 'xai/grok-imagine-image', 'Higgsfield balance', 'Veo 3', 'nano banana pro']) {
    assert.match(text, MODEL_NAMES);
  }
  for (const text of ['This video: about $9', '3 pictures: about $1.17', 'See the breakdown', 'Left in the mood board budget',
    'If the price goes up before we make it, we ask you first.', 'Pebble Cove', 'Make it']) {
    assert.deepEqual(ledgerWords(text), [], text);
    assert.doesNotMatch(text, MODEL_NAMES);
  }
});

test('no seals, stamps, coins or price rings: a price is type, colour and shape', () => {
  for (const name of ['Seal', 'PriceBlock']) {
    assert.ok(!existsSync(join(webDir, 'components/playful', `${name}.tsx`)), `components/playful/${name}.tsx is back`);
  }
  const drawn = /\bpf-(?:seal|stamp|coin|price|confetti)\b/;
  assert.doesNotMatch(readFileSync(join(webDir, 'app/globals.css'), 'utf8'), drawn);
  for (const file of SOURCES) assert.doesNotMatch(readFileSync(join(webDir, file), 'utf8'), drawn, file);
  assert.doesNotMatch(readFileSync(join(webDir, 'components/Checkout.tsx'), 'utf8'),
    /\b(?:bg|text|border|ring)-(?:mustard|coral|red|yellow|amber|orange)\b/, 'the checkout uses no warning colours');
});

test('no stylesheet draws a glyph with content:', () => {
  const sheets = ['app/globals.css', ...readdirSync(join(webDir, 'components'), { recursive: true })
    .filter((file) => file.endsWith('.css')).map((file) => join('components', file))];
  for (const sheet of sheets) {
    for (const [, value] of readFileSync(join(webDir, sheet), 'utf8').matchAll(/(?<![\w-])content:\s*([^;]+);/g)) {
      assert.match(value.trim(), /^(?:""|''|none|normal)$/, `${sheet} draws content: ${value}`);
    }
  }
});

test('no word the owner bans appears on these pages', () => {
  const visible = LITERALS.filter(({ visible }) => visible);
  const problems = visible.flatMap(({ path, text }) => bannedWords(text).map(({ rule, match }) => `${path}: ${rule} ("${match}")`));
  assert.deepEqual(problems, []);
});

test("the site's default description is plain words: no \"content\", no \"harness\"", () => {
  const file = 'app/layout.tsx';
  const source = ts.createSourceFile(file, readFileSync(join(webDir, file), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let description = null;
  const visit = (node) => {
    if (ts.isVariableDeclaration(node) && node.name.getText(source) === 'metadata' && node.initializer && ts.isObjectLiteralExpression(node.initializer)) {
      for (const property of node.initializer.properties) {
        if (!ts.isPropertyAssignment(property) || property.name.getText(source) !== 'description') continue;
        const value = property.initializer;
        if (ts.isStringLiteral(value) || ts.isNoSubstitutionTemplateLiteral(value)) description = value.text;
        else if (value.getText(source) === 'HOME_COPY.meta.description') description = HOME_COPY.meta.description;
        else assert.fail(`the default description is ${value.getText(source)}: a string or the home page's own`);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  assert.ok(description && description.split(/\s+/).length >= 8, `the default description is "${description}"`);
  assert.deepEqual(bannedWords(description), []);
  assert.doesNotMatch(description, ICON_CHARACTERS);
});
