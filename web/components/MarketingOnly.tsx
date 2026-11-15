'use client';

import { usePathname } from 'next/navigation';
import type { ReactNode } from 'react';

/** The studio, the cost dashboard and the login page have their own header (or none). */
const APP_PAGES = /^\/(studio|dashboard|login)(\/|$)/;

export default function MarketingOnly({ children }: { children: ReactNode }) {
  const path = usePathname() ?? '/';
  return APP_PAGES.test(path) ? null : <>{children}</>;
}
