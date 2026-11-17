'use client';

import { Suspense, use } from 'react';
import { useSearchParams } from 'next/navigation';
import { UniverseCreation } from '@/components/UniverseCreation';

function CreationSteps({ id }: { id: string }) {
  const search = useSearchParams();
  return <UniverseCreation seriesId={id} stepParam={search.get('step')} />;
}

/** Making a Universe, one step per screen: /studio/universes/{id}/create?step=spark|references|moodboard|bible|lock. */
export default function UniverseCreatePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  return (
    <Suspense fallback={<p className="mx-auto max-w-[1240px] px-4 py-8 text-[13.5px] text-ink-soft sm:px-8">Loading the Universe…</p>}>
      <CreationSteps id={id} />
    </Suspense>
  );
}
