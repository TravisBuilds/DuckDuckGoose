import { notFound } from 'next/navigation';
import Link from 'next/link';
import { presets, getPresetById, type PresetData } from '@/lib/presets';

interface PageProps {
  params: Promise<{ id: string }>;
}

export async function generateStaticParams() {
  return presets.map((preset) => ({
    id: preset.id,
  }));
}

export async function generateMetadata({ params }: PageProps) {
  const { id } = await params;
  const preset = getPresetById(id);
  
  if (!preset) {
    return {
      title: 'Not Found',
    };
  }

  return {
    title: `${preset.name} - DuckDuckGoose`,
    description: preset.description,
  };
}

function getTemplateDescription(template: string): string {
  const descriptions: Record<string, string> = {
    mood_loop: '1-3 shots with slow movement, seamless loop, music-driven',
    music_montage: '8-30 cinematic shots cut to music with minimal dialogue',
    dialogue_episode: 'Character-driven story with dialogue and shot-reverse-shot coverage',
    continuous_pov: 'One continuous camera path (first-person, orbit, or tracking)',
    cozy_micro: 'Short, intimate shots with ASMR qualities and strong visual style',
    action_sequence: 'Fast-paced choreography with quick cuts or one-take camera work',
    gag_short: 'Single premise building to a punchline or reveal'
  };
  return descriptions[template] || 'Custom video format';
}

function getExampleBeats(preset: PresetData): string[] {
  const beatExamples: Record<string, string[]> = {
    'nostalgia-time-travel': [
      'Opening: Family pulls into parking lot, era-specific signage visible',
      'Inside: Browse shelves, pick up period items (VHS tapes, CDs)',
      'Close-up: Character reaction to finding a favorite from childhood',
      'Final: Walking out with items, warm golden-hour light'
    ],
    'satire-sketch': [
      'Wide: Establish grand setting with invented authority figures',
      'Medium: Characters deliver premise with exaggerated sincerity',
      'Close-up: Reaction shot as the absurdity becomes clear',
      'Punchline: Text card or visual gag lands the satire'
    ],
    'cozy-hangout': [
      'Wide lock-off: Characters gathered in a warm, detailed space',
      'Micro-motion: Rain on windows, steam from mugs, small gestures',
      'Hold: 10-second perfect moment of calm togetherness'
    ],
    'original-mini-movie': [
      'Title card: Establish hero and their goal',
      'Journey: 5-8 stops showing progress and setbacks',
      'Climax: Reaching the destination or achieving the goal',
      'Denouement: Brief aftermath and emotional resolution'
    ]
  };
  
  return beatExamples[preset.id] || [
    'Opening: Establish setting and character(s)',
    'Development: Core action or emotional beats',
    'Resolution: Payoff or emotional landing'
  ];
}

export default async function ArchetypeDetailPage({ params }: PageProps) {
  const { id } = await params;
  const preset = getPresetById(id);

  if (!preset) {
    notFound();
  }

  const templateDesc = getTemplateDescription(preset.template);
  const exampleBeats = getExampleBeats(preset);

  return (
    <main className="flex-1 py-12 px-4 sm:px-6 lg:px-8">
      <div className="max-w-4xl mx-auto">
        <Link
          href="/archetypes"
          className="inline-flex items-center text-sm text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 mb-8"
        >
          ← Back to archetypes
        </Link>

        <div className="mb-8">
          <h1 className="text-4xl font-bold mb-4">{preset.name}</h1>
          <p className="text-xl text-gray-600 dark:text-gray-400">
            {preset.description}
          </p>
        </div>

        <div className="grid md:grid-cols-2 gap-6 mb-8">
          <div className="p-4 bg-gray-50 dark:bg-gray-900 rounded-lg">
            <h3 className="font-semibold mb-2">Format</h3>
            <p className="text-sm text-gray-600 dark:text-gray-400">
              {templateDesc}
            </p>
          </div>
          <div className="p-4 bg-gray-50 dark:bg-gray-900 rounded-lg">
            <h3 className="font-semibold mb-2">Details</h3>
            <ul className="text-sm text-gray-600 dark:text-gray-400 space-y-1">
              <li>Length: {preset.lengthRange[0]}–{preset.lengthRange[1]} seconds</li>
              <li>Aspect: {preset.aspect}</li>
              <li>Voice: {preset.voiceOver}</li>
              <li>Music: {preset.music}</li>
            </ul>
          </div>
        </div>

        <div className="mb-8">
          <h2 className="text-2xl font-semibold mb-4">Example structure</h2>
          <div className="space-y-3">
            {exampleBeats.map((beat, index) => (
              <div key={index} className="p-4 border border-gray-200 dark:border-gray-800 rounded-lg">
                <p className="text-sm">{beat}</p>
              </div>
            ))}
          </div>
        </div>

        <div className="mb-8">
          <h2 className="text-2xl font-semibold mb-4">What you'll provide</h2>
          <div className="flex flex-wrap gap-2">
            {preset.briefFields.map((field) => (
              <span
                key={field}
                className="px-4 py-2 bg-gray-100 dark:bg-gray-800 rounded-lg text-sm font-medium"
              >
                {field.replace(/_/g, ' ')}
              </span>
            ))}
          </div>
          <p className="text-sm text-gray-600 dark:text-gray-400 mt-4">
            Our harness handles shot planning, generation, quality checks, and assembly.
          </p>
        </div>

        <div className="flex flex-wrap gap-2 mb-8">
          {preset.tags.map((tag) => (
            <span
              key={tag}
              className="px-3 py-1 bg-gray-100 dark:bg-gray-800 rounded text-sm"
            >
              {tag}
            </span>
          ))}
        </div>

        <div className="border-t border-gray-200 dark:border-gray-800 pt-8">
          <div className="bg-gray-50 dark:bg-gray-900 rounded-lg p-6">
            <h3 className="text-lg font-semibold mb-2">Ready to create?</h3>
            <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
              Sign up to start generating videos with this archetype. Pricing starts at 340-510 credits per video.
            </p>
            <div className="flex gap-4">
              <button
                disabled
                className="px-6 py-3 bg-gray-300 dark:bg-gray-700 text-gray-500 dark:text-gray-500 rounded-lg font-semibold cursor-not-allowed"
              >
                Create video (coming soon)
              </button>
            </div>
            <p className="text-xs text-gray-500 dark:text-gray-500 mt-4">
              TODO: This button will open the brief form when the backend is ready. 
              Waitlist field below is non-functional in this pass.
            </p>
            <div className="mt-4">
              <div className="flex gap-2">
                <input
                  type="email"
                  placeholder="Join waitlist (not functional yet)"
                  className="flex-1 px-4 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-800 text-sm"
                  disabled
                />
                <button
                  disabled
                  className="px-4 py-2 bg-gray-300 dark:bg-gray-700 text-gray-500 dark:text-gray-500 rounded-lg text-sm font-semibold cursor-not-allowed"
                >
                  Notify me
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </main>
  );
}
