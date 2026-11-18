import type { Metadata } from 'next';
import HomePage from '@/components/home/HomePage';
import { HOME_COPY } from '@/lib/home-copy.mjs';

// Public and static for everyone, signed in or not: the header's "Sign in" and the "Get early access"
// buttons open /studio, which middleware lets a signed-in visitor straight into (components/home/links.ts).
export const metadata: Metadata = {
  title: HOME_COPY.meta.title,
  description: HOME_COPY.meta.description,
  openGraph: {
    type: 'website',
    siteName: HOME_COPY.meta.siteName,
    title: HOME_COPY.meta.title,
    description: HOME_COPY.meta.description,
  },
};

export default function Home() {
  return <HomePage />;
}
