'use client';

import Link from 'next/link';
import { UniverseCreateForm } from '@/components/UniverseCreateForm';
import { card, link } from '@/lib/ui';

export default function NewUniversePage() {
  return (
    <div className="mx-auto max-w-3xl px-4 py-8 sm:px-6 lg:px-8" data-testid="new-universe-page">
      <nav aria-label="Breadcrumb" className="text-sm text-gray-600 dark:text-gray-300">
        <Link href="/studio" className={link}>Universes</Link>
        <span aria-hidden="true"> › </span>
        <span aria-current="page">New Universe</span>
      </nav>
      <div className={`${card} mt-3`}>
        <UniverseCreateForm heading="h1" />
      </div>
    </div>
  );
}
