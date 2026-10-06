import Link from 'next/link';

export default function Home() {
  return (
    <main className="flex-1">
      <section className="py-20 px-4 sm:px-6 lg:px-8">
        <div className="max-w-4xl mx-auto text-center">
          <h1 className="text-5xl sm:text-6xl font-bold mb-6 text-balance">
            Pick a card,<br />get a finished short video
          </h1>
          <p className="text-xl text-gray-600 dark:text-gray-400 mb-8 text-balance">
            DuckDuckGoose is a hands-free AI video harness. Choose your archetype, 
            describe your idea, and let us handle the rest.
          </p>
          <Link
            href="/archetypes"
            className="inline-block px-8 py-4 bg-black dark:bg-white text-white dark:text-black rounded-lg font-semibold hover:bg-gray-800 dark:hover:bg-gray-200 transition-colors"
          >
            Explore archetypes
          </Link>
        </div>
      </section>

      <section className="py-16 px-4 sm:px-6 lg:px-8 bg-gray-50 dark:bg-gray-900">
        <div className="max-w-6xl mx-auto">
          <h2 className="text-3xl font-bold text-center mb-12">How it works</h2>
          <div className="grid md:grid-cols-3 gap-8">
            <div className="text-center">
              <div className="w-16 h-16 bg-gradient-to-br from-purple-500 to-pink-500 rounded-full mx-auto mb-4 flex items-center justify-center text-white font-bold text-2xl">
                1
              </div>
              <h3 className="text-xl font-semibold mb-2">Pick your archetype</h3>
              <p className="text-gray-600 dark:text-gray-400">
                Choose from 24 video genres, from cozy loops to epic journeys
              </p>
            </div>
            <div className="text-center">
              <div className="w-16 h-16 bg-gradient-to-br from-blue-500 to-cyan-500 rounded-full mx-auto mb-4 flex items-center justify-center text-white font-bold text-2xl">
                2
              </div>
              <h3 className="text-xl font-semibold mb-2">Describe your idea</h3>
              <p className="text-gray-600 dark:text-gray-400">
                Fill in a few simple fields about characters, setting, and mood
              </p>
            </div>
            <div className="text-center">
              <div className="w-16 h-16 bg-gradient-to-br from-green-500 to-emerald-500 rounded-full mx-auto mb-4 flex items-center justify-center text-white font-bold text-2xl">
                3
              </div>
              <h3 className="text-xl font-semibold mb-2">Get your video</h3>
              <p className="text-gray-600 dark:text-gray-400">
                Our pipeline handles planning, generation, and assembly for you
              </p>
            </div>
          </div>
        </div>
      </section>

      <section className="py-20 px-4 sm:px-6 lg:px-8">
        <div className="max-w-4xl mx-auto text-center">
          <h2 className="text-3xl font-bold mb-4">Ready to create?</h2>
          <p className="text-gray-600 dark:text-gray-400 mb-8">
            Start with our free trial or explore our pricing options
          </p>
          <div className="flex flex-col sm:flex-row gap-4 justify-center">
            <Link
              href="/archetypes"
              className="px-8 py-4 bg-black dark:bg-white text-white dark:text-black rounded-lg font-semibold hover:bg-gray-800 dark:hover:bg-gray-200 transition-colors"
            >
              Browse archetypes
            </Link>
            <Link
              href="/pricing"
              className="px-8 py-4 border border-gray-300 dark:border-gray-700 rounded-lg font-semibold hover:bg-gray-50 dark:hover:bg-gray-900 transition-colors"
            >
              View pricing
            </Link>
          </div>
        </div>
      </section>
    </main>
  );
}
