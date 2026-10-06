import { presets } from '@/lib/presets';
import ArchetypeCard from '@/components/ArchetypeCard';

export const metadata = {
  title: 'Archetypes - DuckDuckGoose',
  description: 'Browse 24 video archetypes from cozy loops to epic journeys',
};

export default function ArchetypesPage() {
  return (
    <main className="flex-1 py-12 px-4 sm:px-6 lg:px-8">
      <div className="max-w-7xl mx-auto">
        <div className="mb-12">
          <h1 className="text-4xl font-bold mb-4">Video Archetypes</h1>
          <p className="text-xl text-gray-600 dark:text-gray-400">
            Choose your genre and let our harness handle the rest. 
            All content is original—no named IP or franchises.
          </p>
        </div>

        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-6">
          {presets.map((preset) => (
            <ArchetypeCard key={preset.id} preset={preset} />
          ))}
        </div>
      </div>
    </main>
  );
}
