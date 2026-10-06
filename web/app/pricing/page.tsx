import { pricingTiers, creditRate } from '@/lib/pricing';

export const metadata = {
  title: 'Pricing - DuckDuckGoose',
  description: 'Simple, transparent pricing for AI video generation',
};

export default function PricingPage() {
  return (
    <main className="flex-1 py-12 px-4 sm:px-6 lg:px-8">
      <div className="max-w-7xl mx-auto">
        <div className="text-center mb-12">
          <h1 className="text-4xl font-bold mb-4">Simple, transparent pricing</h1>
          <p className="text-xl text-gray-600 dark:text-gray-400 mb-2">
            Pay only for what you create
          </p>
          <p className="text-sm text-gray-500 dark:text-gray-500">
            1 credit = ${creditRate.toFixed(2)} · Credits cover generation, retries, and assembly
          </p>
        </div>

        <div className="grid md:grid-cols-3 gap-8 mb-16">
          {pricingTiers.map((tier) => (
            <div
              key={tier.name}
              className={`rounded-lg border ${
                tier.recommended
                  ? 'border-black dark:border-white shadow-lg scale-105'
                  : 'border-gray-200 dark:border-gray-800'
              } overflow-hidden`}
            >
              {tier.recommended && (
                <div className="bg-black dark:bg-white text-white dark:text-black text-center py-2 text-sm font-semibold">
                  Most popular
                </div>
              )}
              <div className="p-6">
                <h3 className="text-2xl font-bold mb-2">{tier.name}</h3>
                <div className="mb-4">
                  <span className="text-4xl font-bold">${tier.price}</span>
                  <span className="text-gray-600 dark:text-gray-400">/month</span>
                </div>
                <p className="text-sm text-gray-600 dark:text-gray-400 mb-6">
                  {tier.credits.toLocaleString()} credits per month
                </p>
                <ul className="space-y-3 mb-6">
                  {tier.features.map((feature) => (
                    <li key={feature} className="flex items-start gap-2 text-sm">
                      <svg
                        className="w-5 h-5 text-green-500 flex-shrink-0 mt-0.5"
                        fill="none"
                        stroke="currentColor"
                        viewBox="0 0 24 24"
                      >
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          strokeWidth={2}
                          d="M5 13l4 4L19 7"
                        />
                      </svg>
                      <span>{feature}</span>
                    </li>
                  ))}
                </ul>
                <button
                  disabled
                  className="w-full px-6 py-3 bg-gray-300 dark:bg-gray-700 text-gray-500 dark:text-gray-500 rounded-lg font-semibold cursor-not-allowed"
                >
                  Coming soon
                </button>
              </div>
            </div>
          ))}
        </div>

        <div className="border-t border-gray-200 dark:border-gray-800 pt-12">
          <h2 className="text-2xl font-bold mb-8 text-center">How credits work</h2>
          <div className="grid md:grid-cols-2 gap-8 max-w-4xl mx-auto">
            <div className="p-6 bg-gray-50 dark:bg-gray-900 rounded-lg">
              <h3 className="font-semibold mb-3">Credit pricing</h3>
              <ul className="space-y-2 text-sm text-gray-600 dark:text-gray-400">
                <li>• 1 credit = $0.01 at list price</li>
                <li>• Larger plans bring cost down to $0.0084/credit</li>
                <li>• Credits cover all generation, retries, and assembly</li>
                <li>• No hidden fees or per-shot charges</li>
              </ul>
            </div>
            <div className="p-6 bg-gray-50 dark:bg-gray-900 rounded-lg">
              <h3 className="font-semibold mb-3">Typical costs</h3>
              <ul className="space-y-2 text-sm text-gray-600 dark:text-gray-400">
                <li>• Cozy micro (15s): ~510 credits ($5.10)</li>
                <li>• Mood loop (10s): ~340 credits ($3.40)</li>
                <li>• Music montage (45s): ~1,490 credits ($14.90)</li>
                <li>• POV adventure (30s): ~860 credits ($8.60)</li>
              </ul>
            </div>
            <div className="p-6 bg-gray-50 dark:bg-gray-900 rounded-lg">
              <h3 className="font-semibold mb-3">Credit lifetime</h3>
              <ul className="space-y-2 text-sm text-gray-600 dark:text-gray-400">
                <li>• Monthly credits roll over once (up to 2× your grant)</li>
                <li>• Top-up credits valid for 12 months</li>
                <li>• Trial credits expire after 7 days</li>
                <li>• Unused credits spent oldest-first</li>
              </ul>
            </div>
            <div className="p-6 bg-gray-50 dark:bg-gray-900 rounded-lg">
              <h3 className="font-semibold mb-3">Quote & reserve</h3>
              <ul className="space-y-2 text-sm text-gray-600 dark:text-gray-400">
                <li>• Price quoted at the planning stage and locked for 24h</li>
                <li>• Credits reserved before video generation starts</li>
                <li>• Refunded automatically if moderation blocks the video</li>
                <li>• No charges for failed provider jobs</li>
              </ul>
            </div>
          </div>
        </div>

        <div className="mt-12 text-center">
          <p className="text-sm text-gray-500 dark:text-gray-500">
            Payment processing will be handled via Stripe. No payment information is collected in this preview version.
          </p>
        </div>
      </div>
    </main>
  );
}
