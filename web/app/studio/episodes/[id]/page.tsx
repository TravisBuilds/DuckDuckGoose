import { Suspense } from 'react';
import EpisodeUpload from './components/EpisodeUpload';
import ShotsList from './components/ShotsList';
import AuditTrail from './components/AuditTrail';

interface PageProps {
  params: Promise<{ id: string }>;
}

export default async function EpisodeDetailPage({ params }: PageProps) {
  const { id } = await params;

  return (
    <div className="max-w-7xl mx-auto py-8 px-4">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-gray-900">Episode {id}</h1>
        <p className="mt-2 text-gray-600">
          DuckDuckGoose Production Console - Video workflow up to picture lock
        </p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 space-y-6">
          <Suspense fallback={<div className="bg-white rounded-lg border border-gray-200 p-6">Loading...</div>}>
            <EpisodeUpload episodeId={id} />
          </Suspense>

          <Suspense fallback={<div className="bg-white rounded-lg border border-gray-200 p-6">Loading...</div>}>
            <ShotsList episodeId={id} />
          </Suspense>
        </div>

        <div className="space-y-6">
          <Suspense fallback={<div className="bg-white rounded-lg border border-gray-200 p-6">Loading...</div>}>
            <BudgetPanel episodeId={id} />
          </Suspense>

          <Suspense fallback={<div className="bg-white rounded-lg border border-gray-200 p-6">Loading...</div>}>
            <ApprovalsPanel episodeId={id} />
          </Suspense>

          <Suspense fallback={<div className="bg-white rounded-lg border border-gray-200 p-6">Loading...</div>}>
            <AuditTrail episodeId={id} />
          </Suspense>
        </div>
      </div>
    </div>
  );
}

function BudgetPanel({ episodeId }: { episodeId: string }) {
  return (
    <div className="bg-white rounded-lg border border-gray-200 p-6">
      <h3 className="text-lg font-semibold text-gray-900 mb-4">Budget</h3>
      <div className="space-y-3">
        <div className="text-sm text-gray-500">Loading budget data...</div>
      </div>
    </div>
  );
}

function ApprovalsPanel({ episodeId }: { episodeId: string }) {
  return (
    <div className="bg-white rounded-lg border border-gray-200 p-6">
      <h3 className="text-lg font-semibold text-gray-900 mb-4">Approvals</h3>
      <div className="space-y-2">
        <p className="text-sm text-gray-500">Gate approvals will appear here</p>
      </div>
    </div>
  );
}
