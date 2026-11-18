/**
 * The studio's icon set as data: chunky, rounded duotone glyphs on a 24 px grid. Each one is a
 * currentColor outline (navy or azure on light surfaces, white on the deep azure buttons) about
 * 2.25 px thick with round caps and joins, over a soft fill in its concept's own accent, so a
 * Universe, an episode, the cast or a mood board can be told apart at a glance. Small details (a
 * sparkle, a tilt, a blob of colour set off behind the strokes) keep them playful.
 *
 * No glyph draws a person, a face or an animal: members are their initials (Avatar) and the cast is
 * a star with a name tag.
 *
 * components/playful/Icon.tsx draws these shapes; lib/icons.test.mjs checks the set.
 */

/** Accent fills: `fill` for icons at rest or active, `soft` (lighter) for inactive tabs and steps not reached. */
export const ACCENTS = {
  lime: { fill: '#C6F432', soft: '#E6FAA4' },
  yellow: { fill: '#FFD60A', soft: '#FFEB85' },
  orange: { fill: '#FF5A1F', soft: '#FFC1A8' },
  cyan: { fill: '#BDEBFA', soft: '#E2F6FD' },
  sky: { fill: '#9FD3F7', soft: '#D4EAFB' },
};

/** Outline width in grid units at 20 px and up; smaller icons get a little more so they stay chunky. */
export const STROKE = 2.25;

export function strokeFor(size) {
  return Math.round(Math.min(3, Math.max(STROKE, (STROKE * 20) / size)) * 100) / 100;
}

const n = (value) => Math.round(value * 100) / 100;

/** A four-point sparkle with curved sides. */
function sparkle(cx, cy, r) {
  const k = r * 0.16;
  return `M${n(cx)} ${n(cy - r)}Q${n(cx + k)} ${n(cy - k)} ${n(cx + r)} ${n(cy)}Q${n(cx + k)} ${n(cy + k)} ${n(cx)} ${n(cy + r)}`
    + `Q${n(cx - k)} ${n(cy + k)} ${n(cx - r)} ${n(cy)}Q${n(cx - k)} ${n(cy - k)} ${n(cx)} ${n(cy - r)}Z`;
}

/** A plump five-point star, tilted by `tilt` degrees. */
function star(cx, cy, outer, inner, tilt) {
  const points = [];
  for (let i = 0; i < 10; i += 1) {
    const angle = ((-90 + tilt + i * 36) * Math.PI) / 180;
    const r = i % 2 ? inner : outer;
    points.push(`${n(cx + r * Math.cos(angle))} ${n(cy + r * Math.sin(angle))}`);
  }
  return `M${points.join('L')}Z`;
}

const TRAY = 'M3.8 14.4h16.4v3.2c0 1.4-1.2 2.6-2.6 2.6H6.4c-1.4 0-2.6-1.2-2.6-2.6z';
const TRAY_LINE = 'M3.8 14.4v3.2c0 1.4 1.2 2.6 2.6 2.6h11.2c1.4 0 2.6-1.2 2.6-2.6v-3.2';
const LOCK_BODY = { x: 4.4, y: 10.6, width: 15.2, height: 10, rx: 3 };
const PIN = 'M12 20.8s-6.6-5.8-6.6-10.9a6.6 6.6 0 0 1 13.2 0c0 5.1-6.6 10.9-6.6 10.9z';
const PALETTE = 'M12 3.4c-4.9 0-8.6 3.6-8.6 8.4 0 4.6 3.6 8.8 8.1 8.8 1.4 0 2.2-.8 2.2-1.9 0-1.3-1.2-1.6-1.2-2.9 0-1 .8-1.7 1.8-1.7h2.4c2.6 0 4.3-1.7 4.3-4.3 0-3.6-4-6.4-9-6.4z';
const PAGE = 'M6.6 3.4h7.8l4.4 4.4v11.4c0 .8-.6 1.4-1.4 1.4H6.6c-.8 0-1.4-.6-1.4-1.4V4.8c0-.8.6-1.4 1.4-1.4z';
const TRIANGLE = 'M10.2 4.6c.8-1.4 2.8-1.4 3.6 0l7.1 12.4c.8 1.4-.2 3.1-1.8 3.1H4.9c-1.6 0-2.6-1.7-1.8-3.1z';
const PLAY = 'M8.2 5.7c0-1.2 1.3-1.9 2.3-1.3l9.2 6.3c.9.6.9 2 0 2.6l-9.2 6.3c-1 .6-2.3-.1-2.3-1.3z';
const CAST_STAR = star(9.4, 9.7, 7.3, 3.5, -12);
const BOW = 'M12 7.5C10.9 5.3 7.6 4.4 7.6 6.2c0 1.2 2.2 1.3 4.4 1.3zm0 0c1.1-2.2 4.4-3.1 4.4-1.3 0 1.2-2.2 1.3-4.4 1.3z';
const CLAPPER = { x: 3.4, y: 6, width: 17, height: 3.8, rx: 1.3, transform: 'rotate(-12 3.6 9.8)' };
const TAG = { x: 11.4, y: 13.6, width: 9.8, height: 6.8, rx: 1.8, transform: 'rotate(8 16.3 17)' };
const BACK_PHOTO = { x: 8, y: 3.2, width: 12.6, height: 10.2, rx: 2, transform: 'rotate(10 14.3 8.3)' };
const FRONT_TILT = 'rotate(-6 10.1 14.1)';
const FRONT_PHOTO = { x: 3.4, y: 8.6, width: 13.4, height: 11, rx: 2, transform: FRONT_TILT };

/**
 * Layers, painted in order:
 * - fill: the icon's accent; pop: its second accent; lime / yellow / orange / cyan / sky: that accent
 * - paper: white (a page, a tag, a photo's back)
 * - line / bold / thin: the currentColor outline at 1x / 1.2x / 0.6x the stroke
 * - ink: solid currentColor (dots, a keyhole); glint: a white highlight stroke
 */
export const ICONS = {
  home: {
    accent: 'cyan', pop: 'yellow',
    shapes: [
      ['fill', 'path', { d: 'M5.8 10.2 12 5.1l6.2 5.1V19c0 .8-.7 1.5-1.5 1.5H7.3c-.8 0-1.5-.7-1.5-1.5z' }],
      ['pop', 'path', { d: 'M10 20.5v-3.7a2 2 0 0 1 4 0v3.7z' }],
      ['line', 'path', { d: 'M3.4 11.2 12 4.2l8.6 7' }],
      ['line', 'path', { d: 'M5.8 9.6V19c0 .8.7 1.5 1.5 1.5h9.4c.8 0 1.5-.7 1.5-1.5V9.6' }],
      ['line', 'path', { d: 'M10 20.5v-3.7a2 2 0 0 1 4 0v3.7' }],
      ['line', 'path', { d: 'M15.8 7.2V4.6H18v4.4' }],
    ],
  },
  universe: {
    accent: 'sky', pop: 'yellow',
    shapes: [
      ['line', 'ellipse', { cx: 11.4, cy: 13, rx: 9.6, ry: 3.3, transform: 'rotate(-18 11.4 13)' }],
      ['fill', 'circle', { cx: 11.4, cy: 13, r: 6.1 }],
      ['line', 'circle', { cx: 11.4, cy: 13, r: 6.1 }],
      ['line', 'path', { d: 'M1.8 13a9.6 3.3 0 0 0 19.2 0', transform: 'rotate(-18 11.4 13)' }],
      ['pop', 'path', { d: sparkle(19.3, 4.7, 3) }],
      ['thin', 'path', { d: sparkle(19.3, 4.7, 3) }],
    ],
  },
  episode: {
    accent: 'orange',
    shapes: [
      ['fill', 'rect', { x: 3.4, y: 9.8, width: 17.2, height: 10.8, rx: 2.4 }],
      ['line', 'rect', { x: 3.4, y: 9.8, width: 17.2, height: 10.8, rx: 2.4 }],
      ['paper', 'rect', CLAPPER],
      ['line', 'rect', CLAPPER],
      ['thin', 'path', { d: 'M8.6 6.2 6.8 9.6M13 6.2l-1.8 3.4M17.4 6.2l-1.8 3.4', transform: CLAPPER.transform }],
      ['ink', 'path', { d: 'M10.7 12.9v4.6l4-2.3z' }],
    ],
  },
  cast: {
    accent: 'yellow',
    shapes: [
      ['fill', 'path', { d: CAST_STAR }],
      ['line', 'path', { d: CAST_STAR }],
      ['paper', 'rect', TAG],
      ['line', 'rect', TAG],
      ['ink', 'circle', { cx: 13.9, cy: 15.6, r: 0.7, transform: TAG.transform }],
      ['thin', 'path', { d: 'M14.2 18h4.6', transform: TAG.transform }],
    ],
  },
  place: {
    accent: 'lime',
    shapes: [
      ['fill', 'path', { d: PIN }],
      ['line', 'path', { d: PIN }],
      ['paper', 'circle', { cx: 12, cy: 9.8, r: 2.5 }],
      ['line', 'circle', { cx: 12, cy: 9.8, r: 2.5 }],
    ],
  },
  prop: {
    accent: 'orange', pop: 'yellow',
    shapes: [
      ['fill', 'rect', { x: 4.6, y: 11.2, width: 14.8, height: 9.4, rx: 1.8 }],
      ['fill', 'rect', { x: 3.4, y: 7.6, width: 17.2, height: 3.6, rx: 1.3 }],
      ['pop', 'rect', { x: 10.7, y: 7.6, width: 2.6, height: 13 }],
      ['line', 'rect', { x: 4.6, y: 11.2, width: 14.8, height: 9.4, rx: 1.8 }],
      ['line', 'rect', { x: 3.4, y: 7.6, width: 17.2, height: 3.6, rx: 1.3 }],
      ['pop', 'path', { d: BOW }],
      ['line', 'path', { d: BOW }],
    ],
  },
  references: {
    accent: 'cyan',
    shapes: [
      ['paper', 'rect', BACK_PHOTO],
      ['line', 'rect', BACK_PHOTO],
      ['fill', 'rect', FRONT_PHOTO],
      ['line', 'rect', FRONT_PHOTO],
      ['line', 'path', { d: 'M5.8 17.5l3.1-3.1 2.3 2.3 1.5-1.5 2.4 2.4', transform: FRONT_TILT }],
      ['ink', 'circle', { cx: 13.3, cy: 11.7, r: 1.1, transform: FRONT_TILT }],
    ],
  },
  moodboard: {
    accent: 'orange',
    shapes: [
      ['fill', 'path', { d: PALETTE }],
      ['line', 'path', { d: PALETTE }],
      ['lime', 'circle', { cx: 7.4, cy: 12.3, r: 1.7 }],
      ['thin', 'circle', { cx: 7.4, cy: 12.3, r: 1.7 }],
      ['yellow', 'circle', { cx: 8.9, cy: 7.9, r: 1.7 }],
      ['thin', 'circle', { cx: 8.9, cy: 7.9, r: 1.7 }],
      ['cyan', 'circle', { cx: 13.6, cy: 6.8, r: 1.7 }],
      ['thin', 'circle', { cx: 13.6, cy: 6.8, r: 1.7 }],
    ],
  },
  bible: {
    accent: 'sky', pop: 'orange',
    shapes: [
      ['fill', 'path', { d: 'M7 3.4h11.2c.7 0 1.2.5 1.2 1.2v12.6H7c-1.3 0-2.4.8-2.4 1.7V5.8c0-1.3 1.1-2.4 2.4-2.4z' }],
      ['paper', 'path', { d: 'M7 17.2h12.4v3.4H7c-1.3 0-2.4-.8-2.4-1.7s1.1-1.7 2.4-1.7z' }],
      ['pop', 'path', { d: 'M13 3.4v6.6l2-1.5 2 1.5V3.4z' }],
      ['line', 'path', { d: 'M13 3.6v6.4l2-1.5 2 1.5V3.6' }],
      ['line', 'path', { d: 'M4.6 18.9V5.8c0-1.3 1.1-2.4 2.4-2.4h11.2c.7 0 1.2.5 1.2 1.2v16H7c-1.3 0-2.4-.8-2.4-1.7s1.1-1.7 2.4-1.7h12.4' }],
    ],
  },
  lock: {
    accent: 'lime',
    shapes: [
      ['line', 'path', { d: 'M7.9 10.8V8.2a4.1 4.1 0 0 1 8.2 0v2.6' }],
      ['fill', 'rect', LOCK_BODY],
      ['line', 'rect', LOCK_BODY],
      ['ink', 'circle', { cx: 12, cy: 14.6, r: 1.5 }],
      ['line', 'path', { d: 'M12 15.4v2.3' }],
      ['thin', 'path', { d: 'M18.4 5.2 20 3.6M19.2 7.9h2M17.1 3.4V1.6' }],
    ],
  },
  voice: {
    accent: 'cyan',
    shapes: [
      ['fill', 'rect', { x: 8.6, y: 2.8, width: 6.8, height: 11.6, rx: 3.4 }],
      ['line', 'rect', { x: 8.6, y: 2.8, width: 6.8, height: 11.6, rx: 3.4 }],
      ['thin', 'path', { d: 'M8.8 7.4h2.6M8.8 10.2h2.6' }],
      ['line', 'path', { d: 'M5.4 11.4a6.6 6.6 0 0 0 13.2 0M12 18v3.2M8.8 21.2h6.4' }],
      ['thin', 'path', { d: 'M19.6 2.8c.9.8 1.4 1.9 1.4 3.1s-.5 2.3-1.4 3.1' }],
    ],
  },
  music: {
    accent: 'yellow',
    shapes: [
      ['ink', 'path', { d: 'M9.5 6.2l9.6-2.6v3l-9.6 2.6z' }],
      ['line', 'path', { d: 'M9.5 17V6.4M19.1 15V4' }],
      ['fill', 'ellipse', { cx: 7, cy: 17.2, rx: 2.8, ry: 2.2, transform: 'rotate(-20 7 17.2)' }],
      ['line', 'ellipse', { cx: 7, cy: 17.2, rx: 2.8, ry: 2.2, transform: 'rotate(-20 7 17.2)' }],
      ['fill', 'ellipse', { cx: 16.6, cy: 15.2, rx: 2.8, ry: 2.2, transform: 'rotate(-20 16.6 15.2)' }],
      ['line', 'ellipse', { cx: 16.6, cy: 15.2, rx: 2.8, ry: 2.2, transform: 'rotate(-20 16.6 15.2)' }],
    ],
  },
  coins: {
    accent: 'yellow',
    shapes: [
      ['fill', 'circle', { cx: 15.2, cy: 8.8, r: 5.4 }],
      ['line', 'circle', { cx: 15.2, cy: 8.8, r: 5.4 }],
      ['fill', 'circle', { cx: 9.6, cy: 14.4, r: 6.4 }],
      ['line', 'circle', { cx: 9.6, cy: 14.4, r: 6.4 }],
      ['thin', 'path', { d: 'M11.6 12.3c-.4-.6-1.1-.9-1.9-.9-1.1 0-1.9.6-1.9 1.4 0 1.9 4 1.2 4 3.2 0 .9-.9 1.5-2.1 1.5-.9 0-1.7-.4-2.2-1M9.6 10.4v1M9.6 17.5v1' }],
    ],
  },
  download: {
    accent: 'lime',
    shapes: [
      ['fill', 'path', { d: TRAY }],
      ['line', 'path', { d: TRAY_LINE }],
      ['line', 'path', { d: 'M12 3.8v10M7.8 9.6l4.2 4.2 4.2-4.2' }],
    ],
  },
  search: {
    accent: 'cyan',
    shapes: [
      ['fill', 'circle', { cx: 10.3, cy: 10.3, r: 6.3 }],
      ['line', 'circle', { cx: 10.3, cy: 10.3, r: 6.3 }],
      ['glint', 'path', { d: 'M7.3 9.4a3.3 3.3 0 0 1 2.2-2.2' }],
      ['bold', 'path', { d: 'M15.2 15.2l4.8 4.8' }],
    ],
  },
  menu: {
    accent: 'yellow',
    shapes: [
      ['fill', 'circle', { cx: 15.4, cy: 14.6, r: 6.4 }],
      ['line', 'path', { d: 'M4 6.8h16M4 12h11.5M4 17.2h14' }],
    ],
  },
  plus: {
    accent: 'lime',
    shapes: [
      ['fill', 'circle', { cx: 13.4, cy: 13.4, r: 7.2 }],
      ['bold', 'path', { d: 'M12 4.6v14.8M4.6 12h14.8' }],
    ],
  },
  check: {
    accent: 'lime',
    shapes: [
      ['fill', 'circle', { cx: 13.2, cy: 13.4, r: 7.2 }],
      ['bold', 'path', { d: 'M4.8 12.4l4.6 4.6 9.8-9.8' }],
    ],
  },
  arrow: {
    accent: 'lime',
    shapes: [
      ['fill', 'circle', { cx: 11, cy: 13.4, r: 7.2 }],
      ['bold', 'path', { d: 'M4.4 12h14.8M13.4 6.2l5.8 5.8-5.8 5.8' }],
    ],
  },
  alert: {
    accent: 'orange',
    shapes: [
      ['fill', 'path', { d: TRIANGLE }],
      ['line', 'path', { d: TRIANGLE }],
      ['line', 'path', { d: 'M12 9.4v4' }],
      ['ink', 'circle', { cx: 12, cy: 16.9, r: 1.2 }],
    ],
  },
  note: {
    accent: 'sky',
    shapes: [
      ['fill', 'path', { d: PAGE }],
      ['line', 'path', { d: PAGE }],
      ['paper', 'path', { d: 'M14.4 3.6v3c0 .7.6 1.3 1.3 1.3h3z' }],
      ['line', 'path', { d: 'M14.4 3.6v3c0 .7.6 1.3 1.3 1.3h3' }],
      ['line', 'path', { d: 'M8.4 12h7.2M8.4 15.6h4.4' }],
    ],
  },
  spark: {
    accent: 'yellow',
    shapes: [
      ['fill', 'path', { d: sparkle(10.8, 12.6, 8.4) }],
      ['line', 'path', { d: sparkle(10.8, 12.6, 8.4) }],
      ['ink', 'path', { d: sparkle(19.4, 4.6, 2.5) }],
    ],
  },
  photo: {
    accent: 'cyan', pop: 'yellow',
    shapes: [
      ['fill', 'rect', { x: 3.4, y: 4.6, width: 17.2, height: 14.8, rx: 2.6 }],
      ['line', 'rect', { x: 3.4, y: 4.6, width: 17.2, height: 14.8, rx: 2.6 }],
      ['line', 'path', { d: 'M3.8 16.9l4.8-4.8 3.6 3.6 2.4-2.4 5.4 5.4' }],
      ['pop', 'circle', { cx: 15.4, cy: 9.2, r: 1.9 }],
      ['thin', 'circle', { cx: 15.4, cy: 9.2, r: 1.9 }],
    ],
  },
  film: {
    accent: 'sky',
    shapes: [
      ['fill', 'rect', { x: 3.4, y: 4.4, width: 17.2, height: 15.2, rx: 2.4 }],
      ['paper', 'rect', { x: 8, y: 4.4, width: 8, height: 15.2 }],
      ['line', 'rect', { x: 3.4, y: 4.4, width: 17.2, height: 15.2, rx: 2.4 }],
      ['line', 'path', { d: 'M8 4.6v14.8M16 4.6v14.8' }],
      ['ink', 'circle', { cx: 5.7, cy: 8.2, r: 0.8 }],
      ['ink', 'circle', { cx: 5.7, cy: 12, r: 0.8 }],
      ['ink', 'circle', { cx: 5.7, cy: 15.8, r: 0.8 }],
      ['ink', 'circle', { cx: 18.3, cy: 8.2, r: 0.8 }],
      ['ink', 'circle', { cx: 18.3, cy: 12, r: 0.8 }],
      ['ink', 'circle', { cx: 18.3, cy: 15.8, r: 0.8 }],
    ],
  },
  triangle: {
    accent: 'yellow',
    shapes: [
      ['fill', 'path', { d: PLAY }],
      ['line', 'path', { d: PLAY }],
    ],
  },
  exit: {
    accent: 'orange',
    shapes: [
      ['fill', 'path', { d: 'M4.6 5.2c0-.9.7-1.6 1.6-1.6h6.6v16.8H6.2c-.9 0-1.6-.7-1.6-1.6z' }],
      ['line', 'path', { d: 'M12.6 3.6H6.2c-.9 0-1.6.7-1.6 1.6v13.6c0 .9.7 1.6 1.6 1.6h6.4' }],
      ['line', 'path', { d: 'M10 12h10.2M16.4 8.2l3.8 3.8-3.8 3.8' }],
    ],
  },
};

/** The icon on each step bubble, by step key: the Universe path, the video wizard and the episode trail. */
export const STEP_ICON = {
  spark: 'spark',
  references: 'references',
  moodboard: 'moodboard',
  bible: 'bible',
  lock: 'lock',
  episodes: 'episode',
  archetype: 'moodboard',
  idea: 'note',
  review: 'coins',
  episode: 'episode',
  story: 'note',
  cast: 'cast',
  shot_plan: 'film',
  stills: 'photo',
  final_cut: 'episode',
  download: 'download',
};

/** Icons that sit side by side, so each needs its own accent. */
export const ICON_GROUPS = {
  tabBar: ['home', 'episode', 'coins'],
  heroTrio: ['cast', 'place', 'prop'],
  creationPath: ['spark', 'references', 'moodboard', 'bible', 'lock'],
};

/** An outline colour on a surface it sits on; each must keep 3:1 (WCAG 1.4.11) since some icons carry meaning. */
export const OUTLINE_PAIRS = [
  { outline: '#1F2A44', surface: '#FFFFFF', where: 'navy outline on cards' },
  { outline: '#1F2A44', surface: '#EAF6FD', where: 'navy outline on the page' },
  { outline: '#1F2A44', surface: '#DCEFFB', where: 'navy outline on sky headers and soft buttons' },
  { outline: '#1F2A44', surface: '#C6F432', where: 'navy outline on a done step and go buttons' },
  { outline: '#536379', surface: '#FFFFFF', where: 'inactive tab and step-not-reached outline' },
  { outline: '#536379', surface: '#EAF6FD', where: 'inactive outline on the page' },
  { outline: '#0A6CC0', surface: '#FFFFFF', where: 'active tab outline' },
  { outline: '#0A6CC0', surface: '#DCEFFB', where: 'active tab outline on its pill' },
  { outline: '#0A62AD', surface: '#DCEFFB', where: 'soft round buttons' },
  { outline: '#FFFFFF', surface: '#0A6CC0', where: 'white outline on azure buttons, the make button and the current step' },
  { outline: '#B2380A', surface: '#FFE8DE', where: 'a blocked step' },
];

/** Paint attributes for one layer (React prop names); `soft` uses the lighter accent shades. */
export function layerProps(layer, icon, { soft = false, stroke = STROKE } = {}) {
  const shade = (accent) => (accent ? ACCENTS[accent][soft ? 'soft' : 'fill'] : undefined);
  const outline = (width) => ({
    fill: 'none', stroke: 'currentColor', strokeWidth: n(width), strokeLinecap: 'round', strokeLinejoin: 'round',
  });
  switch (layer) {
    case 'fill': return { fill: shade(icon.accent), className: 'pf-ico-fill' };
    case 'pop': return { fill: shade(icon.pop ?? icon.accent), className: 'pf-ico-fill' };
    case 'paper': return { fill: '#FFFFFF' };
    case 'line': return outline(stroke);
    case 'bold': return outline(stroke * 1.2);
    case 'thin': return outline(stroke * 0.6);
    case 'ink': return { fill: 'currentColor', stroke: 'currentColor', strokeWidth: n(stroke * 0.4), strokeLinejoin: 'round' };
    case 'glint': return { fill: 'none', stroke: '#FFFFFF', strokeWidth: n(stroke * 0.75), strokeLinecap: 'round' };
    default:
      if (layer in ACCENTS) return { fill: shade(layer), className: 'pf-ico-fill' };
      throw new Error(`unknown icon layer ${layer}`);
  }
}
