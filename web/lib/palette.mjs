/**
 * The studio palette as data: a light, airy sky-blue look (pale sky page and headers, white cards,
 * an azure primary, lime / orange-red / yellow / cyan accents, navy headings and cool grey text).
 * app/globals.css declares the same values as CSS variables and Tailwind theme colours;
 * lib/palette.test.mjs checks that the two agree and that every text/background pair the studio uses
 * meets WCAG AA (4.5:1).
 *
 * Each hue keeps its job: blue is the primary action, links and the active tab; green (lime) is
 * locked, approved and approve-and-spend; mustard (yellow) is a decision waiting for you; aqua
 * (light cyan) is the studio working; coral (orange-red) is stopped or destructive. The bright
 * swatches are fills, circles and dots: white text goes only on the -deep tokens, which is why the
 * azure buttons are a deeper shade than the bright azure (white on #1E9BF0 is only 3.0:1). The -ink
 * tokens are the text colours on the -soft fills.
 */

export const TOKENS = {
  coral: '#FF5A1F',
  'coral-deep': '#C2410C',
  'coral-soft': '#FFE8DE',
  mustard: '#FFD60A',
  'mustard-deep': '#7D5E00',
  'mustard-soft': '#FFF5C2',
  blue: '#1E9BF0',
  'blue-deep': '#0A6CC0',
  'blue-soft': '#DCEFFB',
  aqua: '#BDEBFA',
  'aqua-deep': '#0A6C88',
  'aqua-soft': '#E4F7FD',
  green: '#C6F432',
  'green-deep': '#3F720C',
  'green-soft': '#EEFBC6',
  lilac: '#A78BFA',
  'lilac-deep': '#6447CF',
  'lilac-soft': '#EFEBFE',
  'coral-ink': '#B2380A',
  'mustard-ink': '#735600',
  'blue-ink': '#0A62AD',
  'aqua-ink': '#0A627C',
  'green-ink': '#3F6A06',
  'lilac-ink': '#5B3FC0',
  ink: '#1F2A44',
  'ink-soft': '#536379',
  /** The public home page's text, from the direction deck: navy headings and a slate grey for body copy. */
  navy: '#10263D',
  'navy-soft': '#4A5B6E',
  line: '#DCE7F2',
  /** Dashed "add something here" borders and the open stops on a path. */
  'line-strong': '#B8CCE0',
  /** The page behind everything. */
  page: '#EAF6FD',
  /** Headers, hero bands and soft panels. */
  sky: '#DCEFFB',
  /** Cards. */
  paper: '#FFFFFF',
  /** A neutral fill: picture placeholders, "not yet" chips. */
  mist: '#EDF3F9',
  white: '#FFFFFF',
  /** A disabled neutral button's fill; its text is ink-soft (readable, while the flat fill says "off"). */
  'off-fill': '#E6ECF2',
  /**
   * A disabled coloured button keeps its hue with the colour drained out (globals.css --pf-off-*), so
   * it reads as "this button, not yet" rather than broken grey: azure for primary (and, lighter, soft),
   * lime for go, orange-red for danger, peach for peach, each with a dark text shade of the same hue.
   */
  'off-blue': '#CFE0F0',
  'off-blue-ink': '#28496B',
  'off-soft': '#E9EFF4',
  'off-green': '#E6EDCB',
  'off-green-ink': '#4B5A1E',
  'off-coral': '#F2DCD2',
  'off-coral-ink': '#7A3418',
  'off-peach': '#F3E5DE',
  'off-peach-ink': '#664335',
};

/**
 * Each cast colour's soft gradient, light end first: the circle behind a character's initials and the
 * band at the top of their card. Names and initials sit on it in ink.
 */
export const CAST_GRADIENTS = {
  blue: ['#E3F2FD', '#9FD3F7'],
  mustard: ['#FFF7D1', '#FFE36B'],
  aqua: ['#E8F8FE', '#BDEBFA'],
  coral: ['#FFEDE5', '#FFB898'],
  green: ['#F3FCD6', '#D5F76A'],
  lilac: ['#F2EEFE', '#CDBEFC'],
  ink: ['#EEF2F7', '#C9D3DF'],
};

/**
 * Tailwind's default colour families the older screens use, re-pointed at the palette (cool greys,
 * the azure, orange-red, lime and yellow), so every leftover panel matches the new look.
 */
export const REMAP = {
  gray: {
    50: '#F5F9FC', 100: '#EDF3F8', 200: '#DCE7F2', 300: '#C3D3E3', 400: '#93A5BA', 500: '#5F7088',
    600: '#536379', 700: '#435269', 800: '#2E3A52', 900: '#1F2A44', 950: '#141C30',
  },
  blue: {
    50: '#F0F8FE', 100: '#DCEFFB', 200: '#BCE1F9', 300: '#8DCDF6', 400: '#4DB2F2', 500: '#1E9BF0',
    600: '#0A6CC0', 700: '#0A5FA8', 800: '#0B4F8A', 900: '#0C3F6C', 950: '#0A2A47',
  },
  red: {
    50: '#FFF4EF', 100: '#FFE8DE', 200: '#FFCDB8', 300: '#FFA987', 400: '#FF8053', 500: '#FF5A1F',
    600: '#C2410C', 700: '#A8360A', 800: '#8A2D0A', 900: '#6E250B', 950: '#451505',
  },
  green: {
    50: '#F8FEE7', 100: '#EEFBC6', 200: '#E2F8A0', 300: '#D6F673', 400: '#CDF552', 500: '#C6F432',
    600: '#3F720C', 700: '#36620A', 800: '#2E5309', 900: '#264309', 950: '#162905',
  },
  amber: {
    50: '#FFFBE8', 100: '#FFF5C2', 200: '#FFEC8F', 300: '#FFE45C', 400: '#FFDD33', 500: '#FFD60A',
    600: '#7D5E00', 700: '#6E5300', 800: '#5E4700', 900: '#4D3A00', 950: '#332600',
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
  // white text: only on the -deep fills (and ink, the role chip)
  ['white', 'blue-deep'], ['white', 'green-deep'], ['white', 'coral-deep'], ['white', 'mustard-deep'],
  ['white', 'aqua-deep'], ['white', 'lilac-deep'], ['white', 'ink'],
  // the lighter top of the azure and orange-red button gradients (globals.css .pf-btn-*)
  ['white', '#1273CC'], ['white', '#C9450E'],
  // body text on the page, headers, cards, inputs and neutral fills
  ['ink', 'page'], ['ink-soft', 'page'], ['ink', 'sky'], ['ink-soft', 'sky'], ['ink', 'paper'],
  ['ink-soft', 'paper'], ['ink', 'white'], ['ink-soft', 'white'], ['ink', 'line'], ['ink', 'mist'],
  ['ink-soft', 'mist'],
  // ink on the bright swatches (lime go buttons and seals, the yellow coin, chips on fills), and the
  // cool grey text that can sit over a hero band's lime and cyan circles
  ['ink', 'mustard'], ['ink', 'aqua'], ['ink', 'green'], ['ink-soft', 'green'], ['ink-soft', 'aqua'],
  // the peach button's lighter and deeper ends
  ['ink', '#FFE8DE'], ['ink', '#FFD0BC'],
  // chips and soft panels: the -ink shade on its -soft fill, ink and ink-soft on every soft fill
  ['coral-ink', 'coral-soft'], ['mustard-ink', 'mustard-soft'], ['blue-ink', 'blue-soft'],
  ['aqua-ink', 'aqua-soft'], ['green-ink', 'green-soft'], ['lilac-ink', 'lilac-soft'],
  ['ink', 'coral-soft'], ['ink', 'mustard-soft'], ['ink', 'blue-soft'], ['ink', 'aqua-soft'],
  ['ink', 'green-soft'], ['ink', 'lilac-soft'],
  ['ink-soft', 'coral-soft'], ['ink-soft', 'mustard-soft'], ['ink-soft', 'blue-soft'],
  ['ink-soft', 'aqua-soft'], ['ink-soft', 'green-soft'], ['ink-soft', 'lilac-soft'],
  // the home page's navy and slate text on the page, its sky bands and white cards
  ['navy', 'page'], ['navy', 'sky'], ['navy', 'paper'], ['navy-soft', 'page'], ['navy-soft', 'sky'],
  ['navy-soft', 'paper'],
  // coloured text on the page, headers and cards (links, warnings, prices)
  ['blue-deep', 'page'], ['blue-deep', 'paper'], ['blue-deep', 'white'], ['blue-deep', 'sky'],
  ['blue-ink', 'page'], ['blue-ink', 'sky'], ['coral-deep', 'page'], ['coral-deep', 'paper'],
  ['coral-deep', 'white'], ['green-ink', 'page'], ['green-ink', 'paper'], ['mustard-ink', 'page'],
  ['mustard-ink', 'paper'], ['aqua-ink', 'paper'], ['lilac-deep', 'paper'],
  // a disabled button: ink-soft on the flat off fill (neutral buttons, and white ones with a rim), and
  // each coloured variant's drained fill with its text shade
  ['ink-soft', 'off-fill'], ['off-blue-ink', 'off-blue'], ['off-blue-ink', 'off-soft'], ['off-green-ink', 'off-green'],
  ['off-coral-ink', 'off-coral'], ['off-peach-ink', 'off-peach'],
  // the remapped Tailwind shades the older panels use
  ['gray/500', 'white'], ['gray/600', 'white'], ['gray/700', 'white'], ['gray/600', 'gray/50'],
  ['gray/700', 'gray/100'], ['gray/600', 'page'], ['gray/700', 'paper'], ['gray/900', 'gray/100'],
  ['white', 'blue/600'], ['white', 'blue/700'], ['white', 'red/600'], ['white', 'red/700'],
  ['white', 'green/600'], ['white', 'green/700'], ['white', 'gray/900'],
  ['blue/700', 'white'], ['blue/700', 'blue/50'], ['blue/800', 'blue/100'], ['blue/900', 'blue/50'],
  ['red/700', 'white'], ['red/700', 'red/50'], ['red/800', 'red/50'], ['red/800', 'red/100'],
  ['green/700', 'white'], ['green/800', 'green/50'], ['green/800', 'green/100'],
  ['amber/700', 'white'], ['amber/800', 'amber/50'], ['amber/800', 'amber/100'], ['amber/900', 'amber/50'],
  ['yellow/800', 'yellow/50'], ['yellow/900', 'yellow/50'],
].map(([text, background]) => ({ text, background, ratio: contrastRatio(colour(text), colour(background)) }));
