import Link from 'next/link';
import type { PresetData } from '@/lib/presets';

interface ArchetypeCardProps {
  preset: PresetData;
}

function getGradientForTemplate(template: string): string {
  const gradients: Record<string, string> = {
    mood_loop: 'from-purple-500 to-pink-500',
    music_montage: 'from-blue-500 to-cyan-500',
    dialogue_episode: 'from-orange-500 to-red-500',
    continuous_pov: 'from-green-500 to-emerald-500',
    cozy_micro: 'from-yellow-500 to-orange-400',
    action_sequence: 'from-red-600 to-rose-700',
    gag_short: 'from-indigo-500 to-purple-500'
  };
  return gradients[template] || 'from-gray-500 to-gray-700';
}

export default function ArchetypeCard({ preset }: ArchetypeCardProps) {
  const gradient = getGradientForTemplate(preset.template);
  
  return (
    <Link
      href={`/archetypes/${preset.id}`}
      className="group block rounded-lg border border-gray-200 dark:border-gray-800 overflow-hidden hover:border-gray-300 dark:hover:border-gray-700 transition-colors"
    >
      <div className={`h-32 bg-gradient-to-br ${gradient} relative overflow-hidden`}>
        <div className="absolute inset-0 opacity-0 group-hover:opacity-10 bg-white transition-opacity" />
        <svg
          className="absolute inset-0 w-full h-full opacity-20"
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
        >
          <path
            d={`M0,50 Q25,${Math.random() * 30 + 35} 50,50 T100,50 L100,100 L0,100 Z`}
            fill="rgba(255,255,255,0.3)"
          />
        </svg>
      </div>
      <div className="p-4">
        <h3 className="font-semibold text-lg mb-1">{preset.name}</h3>
        <p className="text-sm text-gray-600 dark:text-gray-400 mb-3">
          {preset.description}
        </p>
        <div className="flex flex-wrap gap-2 mb-3">
          {preset.tags.slice(0, 3).map((tag) => (
            <span
              key={tag}
              className="text-xs px-2 py-1 bg-gray-100 dark:bg-gray-800 rounded"
            >
              {tag}
            </span>
          ))}
        </div>
        <div className="text-xs text-gray-500 dark:text-gray-500">
          {preset.lengthRange[0]}–{preset.lengthRange[1]}s · {preset.aspect}
        </div>
      </div>
    </Link>
  );
}
