/**
 * Every word on the public home page (app/page.tsx and components/home/), from the direction deck.
 * The home components render text only from here, so lib/home-copy.test.mjs can hold all of it to
 * the copy rules (lib/copy-rules.mjs); that test also fails if a component hard-codes visible text.
 */

const CTA = 'Get early access';

export const HOME_COPY = {
  meta: {
    siteName: 'DuckDuckGoose',
    title: 'DuckDuckGoose: Your life, back',
    description:
      'AI that does the work, so you can live the life you want. We start with video: set your face, voice and world once, and every video looks and sounds like you.',
  },
  brand: { first: 'DuckDuck', second: 'Goose' },
  header: {
    skip: 'Skip to main section',
    nav: 'Main',
    signIn: 'Sign in',
    cta: CTA,
  },
  hero: {
    eyebrow: 'Meet DuckDuckGoose',
    title: 'Your life, back',
    lead: 'AI that does the work, so you can live the life you want.',
    cta: CTA,
  },
  stories: {
    eyebrow: 'Why now',
    title: "It's already happening",
    lead: 'AI is about to give people their time back.',
    body: 'Our job is simple: take work off your plate, or turn what you love into money and a following.',
    items: [
      {
        key: 'driver',
        title: 'The driver',
        text: 'A self-driving car means longer, safer shifts, more money, and energy left after work.',
        alt: 'A silver car driving down an open desert road at sunset',
      },
      {
        key: 'dancer',
        title: 'The dancer',
        text: 'A passion becomes a community, then a following, then sold-out shows.',
        alt: 'A dancer leaping across a sunlit studio with tall windows',
      },
    ],
  },
  video: {
    eyebrow: 'Where we start',
    title: 'We start with video',
    lead: 'People who make things feel it first.',
    body: "A real estate agent stars in funny videos of themselves inside the home they're selling. Views jump. No crew, no editing.",
    card: {
      title: 'Your face, your voice, your world.',
      text: 'Set it once. Every video looks and sounds like you.',
    },
    closer: 'Everyone gets their own studio.',
    alt: 'A phone on a tripod filming a bright, freshly styled living room',
  },
  how: {
    eyebrow: 'How it works',
    title: 'Be the star of your own story',
    body: 'Bring yourself, or the world you dream up. We take it from there.',
    steps: [
      { title: 'You make it', text: 'Your idea, your face, your voice.' },
      { title: 'We handle the rest', text: 'Writing, filming, sound and editing.' },
      { title: 'You grow and get paid', text: 'More views, a bigger following, new income.' },
    ],
    alt: 'A small stage with paper clouds, string lights and a microphone, and a phone on a tripod ready to film',
  },
  vision: {
    eyebrow: 'What comes next',
    title: 'From video to everything',
    body: 'Video is step one. Next, we help with more of your work and the things you love.',
    promise: 'Everything we build should give you time back, or turn a passion into income.',
    mission: 'Help everyone make a living doing what they love.',
    signature: 'DuckDuckGoose',
    cta: CTA,
    alt: 'Three kites flying over a green meadow with a winding path, under a bright blue sky',
  },
  footer: {
    copyright: '© 2026 DuckDuckGoose',
    comingSoon: ['Terms (coming soon)', 'Privacy (coming soon)'],
    signIn: 'Sign in',
  },
};

/** Every string in a copy object, with where it is (for the copy-rules test's messages). */
export function copyStrings(value, path = 'HOME_COPY') {
  if (typeof value === 'string') return [{ path, text: value }];
  if (Array.isArray(value)) return value.flatMap((item, i) => copyStrings(item, `${path}[${i}]`));
  if (value && typeof value === 'object') {
    return Object.entries(value).flatMap(([key, item]) => copyStrings(item, `${path}.${key}`));
  }
  return [];
}
