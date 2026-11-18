export interface HomeStory {
  key: 'driver' | 'dancer';
  title: string;
  text: string;
  alt: string;
}

export interface HomeStep {
  title: string;
  text: string;
}

export declare const HOME_COPY: {
  meta: { siteName: string; title: string; description: string };
  brand: { first: string; second: string };
  header: { skip: string; nav: string; signIn: string; cta: string };
  hero: { eyebrow: string; title: string; lead: string; cta: string };
  stories: { eyebrow: string; title: string; lead: string; body: string; items: HomeStory[] };
  video: {
    eyebrow: string;
    title: string;
    lead: string;
    body: string;
    card: { title: string; text: string };
    closer: string;
    alt: string;
  };
  how: { eyebrow: string; title: string; body: string; steps: HomeStep[]; alt: string };
  vision: {
    eyebrow: string;
    title: string;
    body: string;
    promise: string;
    mission: string;
    signature: string;
    cta: string;
    alt: string;
  };
  footer: { copyright: string; comingSoon: string[]; signIn: string };
};

export declare function copyStrings(value: unknown, path?: string): { path: string; text: string }[];
