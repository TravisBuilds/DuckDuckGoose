import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { TOKENS, REMAP, TEXT_PAIRS, contrastRatio } from './palette.mjs';

const webDir = fileURLToPath(new URL('..', import.meta.url));
const css = readFileSync(join(webDir, 'app/globals.css'), 'utf8');
const HUES = ['coral', 'mustard', 'blue', 'aqua', 'green', 'lilac'];

test('every text pair the studio uses meets WCAG AA (at least 4.5:1)', () => {
  assert.ok(TEXT_PAIRS.length > 50);
  for (const pair of TEXT_PAIRS) {
    assert.ok(pair.ratio >= 4.5, `${pair.text} on ${pair.background} is only ${pair.ratio.toFixed(2)}:1`);
  }
});

test('white text works on every -deep token and on none of the bright swatches', () => {
  for (const hue of HUES) {
    assert.ok(contrastRatio('#FFFFFF', TOKENS[`${hue}-deep`]) >= 4.5, `white on ${hue}-deep`);
    assert.ok(contrastRatio('#FFFFFF', TOKENS[hue]) < 4.5, `${hue} is a fill, never a white-text background`);
  }
});

test('each -ink text colour is readable on its -soft fill', () => {
  for (const hue of HUES) {
    const ratio = contrastRatio(TOKENS[`${hue}-ink`], TOKENS[`${hue}-soft`]);
    assert.ok(ratio >= 4.5, `${hue}-ink on ${hue}-soft is ${ratio.toFixed(2)}:1`);
  }
});

test('the spec contrast numbers hold (DESIGN-TARGETS-v2 §1.2)', () => {
  const close = (a, b, label) => assert.ok(Math.abs(a - b) < 0.06, `${label}: ${a.toFixed(2)} vs ${b}`);
  close(contrastRatio('#FFFFFF', TOKENS['blue-deep']), 5.98, 'white on blue-deep');
  close(contrastRatio('#FFFFFF', TOKENS['green-deep']), 5.13, 'white on green-deep');
  close(contrastRatio('#FFFFFF', TOKENS['coral-deep']), 5.69, 'white on coral-deep');
  close(contrastRatio(TOKENS.ink, TOKENS.mustard), 7.72, 'ink on mustard');
  close(contrastRatio(TOKENS.ink, TOKENS.cream), 13.5, 'ink on cream');
  close(contrastRatio(TOKENS['ink-soft'], TOKENS.cream), 5.94, 'ink-soft on cream');
});

test('globals.css declares exactly the palette in lib/palette.mjs', () => {
  for (const [name, hex] of Object.entries(TOKENS)) {
    if (name === 'white') continue;
    const match = css.match(new RegExp(`--${name}:\\s*(#[0-9A-Fa-f]{6})\\s*;`));
    assert.ok(match, `--${name} is missing from globals.css`);
    assert.equal(match[1].toUpperCase(), hex.toUpperCase(), `--${name}`);
  }
  for (const [family, shades] of Object.entries(REMAP)) {
    for (const [shade, hex] of Object.entries(shades)) {
      const match = css.match(new RegExp(`--color-${family}-${shade}:\\s*(#[0-9A-Fa-f]{6})\\s*;`));
      assert.ok(match, `--color-${family}-${shade} is missing from globals.css`);
      assert.equal(match[1].toUpperCase(), hex.toUpperCase(), `--color-${family}-${shade}`);
    }
  }
});

/** Hue (degrees) and HSV saturation (0 to 1) of a #RRGGBB colour. */
function hueAndSaturation(hex) {
  const n = parseInt(hex.replace('#', ''), 16);
  const [r, g, b] = [n >> 16, (n >> 8) & 255, n & 255].map((c) => c / 255);
  const max = Math.max(r, g, b);
  const span = max - Math.min(r, g, b);
  let hue = 0;
  if (span > 0) {
    if (max === r) hue = ((g - b) / span) % 6;
    else if (max === g) hue = (b - r) / span + 2;
    else hue = (r - g) / span + 4;
  }
  return { hue: (hue * 60 + 360) % 360, saturation: max === 0 ? 0 : span / max };
}

test('a disabled coloured button keeps its hue with less colour, and globals.css uses it', () => {
  const variants = [
    { variant: 'primary', live: 'blue-deep', fill: 'off-blue', text: 'off-blue-ink' },
    { variant: 'soft', live: 'blue-soft', fill: 'off-soft', text: 'off-blue-ink' },
    { variant: 'go', live: 'green-deep', fill: 'off-green', text: 'off-green-ink' },
    { variant: 'danger', live: 'coral-deep', fill: 'off-coral', text: 'off-coral-ink' },
    { variant: 'peach', live: '#F6CFA8', fill: 'off-peach', text: 'off-peach-ink' },
  ];
  for (const { variant, live, fill, text } of variants) {
    const on = hueAndSaturation(live.startsWith('#') ? live : TOKENS[live]);
    const off = hueAndSaturation(TOKENS[fill]);
    const turn = Math.min(Math.abs(on.hue - off.hue), 360 - Math.abs(on.hue - off.hue));
    assert.ok(turn <= 15, `${fill} is ${turn.toFixed(0)}° away from ${variant}'s hue`);
    assert.ok(off.saturation < on.saturation, `${fill} is no paler in colour than ${variant}`);
    assert.ok(contrastRatio(TOKENS[text], TOKENS[fill]) >= 4.5, `${text} on ${fill}`);
    const rule = css.match(new RegExp(`\\.pf-btn-${variant}\\s*\\{([^}]*)\\}`));
    assert.ok(rule, `.pf-btn-${variant} is missing`);
    assert.match(rule[1], new RegExp(`--pf-off-bg:\\s*var\\(--${fill}\\);`), `.pf-btn-${variant} off fill`);
    assert.match(rule[1], new RegExp(`--pf-off-fg:\\s*var\\(--${text}\\);`), `.pf-btn-${variant} off text`);
  }
  assert.match(css, /\.pf-btn:disabled,\s*\.pf-btn\[aria-disabled="true"\]\s*\{[^}]*background:\s*var\(--pf-off-bg, var\(--off-fill\)\);[^}]*color:\s*var\(--pf-off-fg, var\(--ink-soft\)\);/);
});

test('light-first: no dark colour scheme, and dark: utilities never switch on', () => {
  assert.doesNotMatch(css, /prefers-color-scheme:\s*dark/);
  assert.match(css, /@custom-variant dark \(&:where\(\.dark, \.dark \*\)\);/);
});

test('the focus ring is a 3px blue-deep outline with a 2px offset', () => {
  assert.match(css, /:focus-visible\s*\{\s*outline:\s*3px solid var\(--blue-deep\);\s*outline-offset:\s*2px;/);
});

// White text on a fill lighter than the -deep shades fails AA. Scan every class list in the app
// for an unprefixed text-white next to an unprefixed bright fill or gradient stop.
const BRIGHT_FILL = new RegExp(
  `^(bg|from|via|to)-(?:(?:${HUES.join('|')})(?:-soft)?|(?:gray|blue|red|green|amber|yellow|purple|pink|cyan|emerald|orange|sky|teal)-(?:50|100|200|300|400|500)|white)$`,
);

function sourceFiles(dir) {
  return readdirSync(join(webDir, dir), { recursive: true })
    .filter((file) => /\.(tsx|ts)$/.test(file))
    .map((file) => join(webDir, dir, file));
}

test('white text sits only on -deep (or darker) fills across the app', () => {
  const offenders = [];
  for (const file of [...sourceFiles('app'), ...sourceFiles('components')]) {
    const source = readFileSync(file, 'utf8');
    for (const [, classes] of source.matchAll(/["'`]([^"'`]*\btext-white\b[^"'`]*)["'`]/g)) {
      const base = classes.split(/\s+/).filter((name) => name && !name.includes(':'));
      if (!base.includes('text-white')) continue;
      const bright = base.filter((name) => BRIGHT_FILL.test(name));
      if (bright.length) offenders.push(`${file.slice(webDir.length)}: ${bright.join(' ')} + text-white`);
    }
  }
  assert.deepEqual(offenders, []);
});
