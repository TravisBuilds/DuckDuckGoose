/**
 * The "Playful Universe" palette (docs: DESIGN-TARGETS-v2 §1.1) as data. app/globals.css declares the
 * same values as CSS variables and Tailwind theme colours; lib/palette.test.mjs checks that the two
 * agree and that every text/background pair the studio uses meets WCAG AA (4.5:1).
 *
 * White text goes only on the -deep tokens. The -ink tokens are the text colours on the -soft fills:
 * the spec's -deep on -soft pairs for mustard (4.49), aqua (4.41) and green (4.45) fall just short of
 * 4.5:1, so chips on those fills use a slightly darker shade of the same hue.
 */

export const TOKENS = {
  coral: '#E8534A',
  'coral-deep': '#B83A31',
  'coral-soft': '#FDE3E0',
  mustard: '#D9BC6E',
  'mustard-deep': '#8A6A12',
  'mustard-soft': '#FBF1D6',
  blue: '#4F86C6',
  'blue-deep': '#2F64A8',
  'blue-soft': '#E1ECF8',
  aqua: '#77C6D3',
  'aqua-deep': '#1F7A88',
  'aqua-soft': '#E2F4F7',
  green: '#4CAF50',
  'green-deep': '#2E7D32',
  'green-soft': '#E3F3E4',
  lilac: '#9B7FD1',
  'lilac-deep': '#6A4FB0',
  'lilac-soft': '#EEE8F9',
  'coral-ink': '#B83A31',
  'mustard-ink': '#806311',
  'blue-ink': '#2F64A8',
  'aqua-ink': '#1D717E',
  'green-ink': '#2B742F',
  'lilac-ink': '#6A4FB0',
  ink: '#1F2A44',
  'ink-soft': '#56607A',
  line: '#E6E1D6',
  cream: '#FFF8EC',
  paper: '#FFFDF8',
  'sky-top': '#9FD3EE',
  'sky-bottom': '#E6F5FC',
  wood: '#E3A963',
  'wood-dark': '#C98942',
  white: '#FFFFFF',
  /** A disabled button's fill; its text is ink-soft (readable, while the flat fill says "off"). */
  'off-fill': '#E7E3DA',
};

/**
 * Tailwind's default colour families the older screens use, re-pointed at the palette (warm greys,
 * the palette's blue, coral, green and mustard), so every leftover panel matches the new look.
 */
export const REMAP = {
  gray: {
    50: '#FBF8F2', 100: '#F4F0E8', 200: '#E6E1D6', 300: '#D7D2C6', 400: '#A9A396', 500: '#646B80',
    600: '#56607A', 700: '#444D66', 800: '#2E3852', 900: '#1F2A44', 950: '#141C30',
  },
  blue: {
    50: '#F0F5FB', 100: '#E1ECF8', 200: '#C6D9F0', 300: '#9DBDE4', 400: '#74A0D5', 500: '#4F86C6',
    600: '#2F64A8', 700: '#285A99', 800: '#234D82', 900: '#1B3B64', 950: '#12284A',
  },
  red: {
    50: '#FEF2F0', 100: '#FDE3E0', 200: '#F9C9C3', 300: '#F2A49B', 400: '#EC7A70', 500: '#E8534A',
    600: '#B83A31', 700: '#A3332B', 800: '#8A2B24', 900: '#6E221D', 950: '#4A1713',
  },
  green: {
    50: '#F1F9F1', 100: '#E3F3E4', 200: '#C5E5C7', 300: '#9CD29F', 400: '#72C076', 500: '#4CAF50',
    600: '#2B742F', 700: '#276B2B', 800: '#245F28', 900: '#1C4A1F', 950: '#12321A',
  },
  amber: {
    50: '#FDF8EA', 100: '#FBF1D6', 200: '#F4E2B2', 300: '#EACF8A', 400: '#E2C57C', 500: '#D9BC6E',
    600: '#8A6A12', 700: '#7A5D10', 800: '#6A510E', 900: '#573F0B', 950: '#3E3008',
  },
};
REMAP.yellow = REMAP.amber;

/** Relative luminance (WCAG 2.1) of a #RRGGBB colour. */
export function luminance(hex) {
  const n = parseInt(String(hex).replace('#', ''), 16);
  return [n >> 16, (n >> 8) & 255, n & 255]
    .map((c) => {
      const s = c / 255;
      return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    })
    .reduce((sum, c, i) => sum + c * [0.2126, 0.7152, 0.0722][i], 0);
}

/** WCAG contrast ratio of two #RRGGBB colours (1 to 21). */
export function contrastRatio(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function colour(name) {
  if (name.startsWith('#')) return name;
  const [family, shade] = name.split('/');
  if (shade) return REMAP[family][shade];
  if (!(name in TOKENS)) throw new Error(`unknown colour ${name}`);
  return TOKENS[name];
}

/** [text, background] as the studio uses them: tokens by name, remapped Tailwind shades as "gray/700". */
export const TEXT_PAIRS = [
  // white text: only on the -deep fills (and ink, the selected tab)
  ['white', 'blue-deep'], ['white', 'green-deep'], ['white', 'coral-deep'], ['white', 'mustard-deep'],
  ['white', 'aqua-deep'], ['white', 'lilac-deep'], ['white', 'ink'],
  // the lighter top of the blue, green and coral button gradients (globals.css .pf-btn-*)
  ['white', '#3B73B8'], ['white', '#338637'], ['white', '#C4433A'],
  // body text on the page, cards and inputs
  ['ink', 'cream'], ['ink-soft', 'cream'], ['ink', 'paper'], ['ink-soft', 'paper'], ['ink', 'white'],
  ['ink-soft', 'white'], ['ink', 'line'],
  // ink on the bright swatches (seal text, current stop, chips on fills)
  ['ink', 'mustard'], ['ink', 'aqua'], ['ink', 'green'],
  // chips and soft panels: the -ink shade on its -soft fill, ink and ink-soft on every soft fill
  ['coral-ink', 'coral-soft'], ['mustard-ink', 'mustard-soft'], ['blue-ink', 'blue-soft'],
  ['aqua-ink', 'aqua-soft'], ['green-ink', 'green-soft'], ['lilac-ink', 'lilac-soft'],
  ['ink', 'coral-soft'], ['ink', 'mustard-soft'], ['ink', 'blue-soft'], ['ink', 'aqua-soft'],
  ['ink', 'green-soft'], ['ink', 'lilac-soft'],
  ['ink-soft', 'coral-soft'], ['ink-soft', 'mustard-soft'], ['ink-soft', 'blue-soft'],
  ['ink-soft', 'aqua-soft'], ['ink-soft', 'green-soft'],
  // coloured text on the page and cards (links, warnings, prices)
  ['blue-deep', 'cream'], ['blue-deep', 'paper'], ['blue-deep', 'white'], ['coral-deep', 'cream'],
  ['coral-deep', 'paper'], ['coral-deep', 'white'], ['green-ink', 'cream'], ['green-ink', 'paper'],
  ['mustard-ink', 'cream'], ['mustard-ink', 'paper'], ['aqua-ink', 'paper'], ['lilac-deep', 'paper'],
  // the scene: text on the sky, the floor and the glass fallback
  ['ink', 'sky-top'], ['ink', 'sky-bottom'], ['ink-soft', 'sky-bottom'], ['ink', 'wood'],
  // a disabled button: ink-soft on the flat off fill
  ['ink-soft', 'off-fill'],
  // the remapped Tailwind shades the older panels use
  ['gray/500', 'white'], ['gray/600', 'white'], ['gray/700', 'white'], ['gray/600', 'gray/50'],
  ['gray/700', 'gray/100'], ['gray/600', 'cream'], ['gray/700', 'paper'], ['gray/900', 'gray/100'],
  ['white', 'blue/600'], ['white', 'blue/700'], ['white', 'red/600'], ['white', 'red/700'],
  ['white', 'green/600'], ['white', 'green/700'], ['white', 'gray/900'],
  ['blue/700', 'white'], ['blue/700', 'blue/50'], ['blue/800', 'blue/100'], ['blue/900', 'blue/50'],
  ['red/700', 'white'], ['red/700', 'red/50'], ['red/800', 'red/50'], ['red/800', 'red/100'],
  ['green/700', 'white'], ['green/800', 'green/50'], ['green/800', 'green/100'],
  ['amber/700', 'white'], ['amber/800', 'amber/50'], ['amber/800', 'amber/100'], ['amber/900', 'amber/50'],
  ['yellow/800', 'yellow/50'], ['yellow/900', 'yellow/50'],
].map(([text, background]) => ({ text, background, ratio: contrastRatio(colour(text), colour(background)) }));
