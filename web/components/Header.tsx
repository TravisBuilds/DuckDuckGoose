import Link from 'next/link';
import { btnPrimary } from '@/lib/ui';

export default function Header() {
  return (
    <header className="border-b border-gray-200 dark:border-gray-800">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex justify-between items-center h-16">
          <div className="flex items-center gap-8">
            <Link href="/" className="text-xl font-bold">
              DuckDuckGoose
            </Link>
            <nav className="hidden md:flex gap-6">
              <Link href="/archetypes" className="text-sm hover:text-gray-600 dark:hover:text-gray-300">
                Archetypes
              </Link>
              <Link href="/pricing" className="text-sm hover:text-gray-600 dark:hover:text-gray-300">
                Pricing
              </Link>
              <Link href="/studio" prefetch={false} className="text-sm hover:text-gray-600 dark:hover:text-gray-300">
                Studio
              </Link>
              <Link href="/dashboard" prefetch={false} className="text-sm hover:text-gray-600 dark:hover:text-gray-300">
                Dashboard
              </Link>
            </nav>
          </div>
          <div className="flex items-center gap-4">
            <Link href="/studio" prefetch={false} className={btnPrimary}>
              Open the studio
            </Link>
          </div>
        </div>
      </div>
    </header>
  );
}
