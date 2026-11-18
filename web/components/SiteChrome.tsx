'use client';

import { usePathname } from 'next/navigation';
import Footer from '@/components/Footer';
import Header from '@/components/Header';
import MarketingOnly from '@/components/MarketingOnly';

/**
 * The shared header and footer, left out on the home page, which has its own (components/home/).
 * They are imported here rather than passed in as children so that the home page does not carry
 * them in its server payload either.
 */
const onHome = (path: string | null) => (path ?? '/') === '/';

export function SiteHeader() {
  return onHome(usePathname()) ? null : (
    <MarketingOnly>
      <Header />
    </MarketingOnly>
  );
}

export function SiteFooter() {
  return onHome(usePathname()) ? null : <Footer />;
}
