'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';

export default function StudioHomePage() {
  const [apiHealth, setApiHealth] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const router = useRouter();

  useEffect(() => {
    checkAPIHealth();
  }, []);

  const checkAPIHealth = async () => {
    try {
      // Get admin secret from cookie
      const secret = document.cookie
        .split('; ')
        .find(row => row.startsWith('studio_admin_token='))
        ?.split('=')[1];

      if (!secret) {
        router.push('/studio/login');
        return;
      }

      const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
      
      const response = await fetch(`${apiUrl}/api/health`, {
        headers: {
          'Authorization': `Bearer ${secret}`,
        },
      });

      if (!response.ok) {
        throw new Error('API health check failed');
      }

      const data = await response.json();
      setApiHealth(data);
    } catch (error) {
      console.error('API health check failed:', error);
      setApiHealth({ status: 'error', error: String(error) });
    } finally {
      setLoading(false);
    }
  };

  const handleLogout = () => {
    document.cookie = 'studio_admin_token=; path=/; max-age=0';
    router.push('/');
  };

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-center">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-black dark:border-white mx-auto mb-4"></div>
          <p className="text-gray-600 dark:text-gray-400">Loading studio...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900">
      <header className="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4">
          <div className="flex justify-between items-center">
            <div>
              <h1 className="text-2xl font-bold">Studio Console</h1>
              <p className="text-sm text-gray-600 dark:text-gray-400">
                DuckDuckGoose Production Pipeline
              </p>
            </div>
            <div className="flex items-center gap-4">
              <Link
                href="/"
                className="text-sm text-gray-600 dark:text-gray-400 hover:text-black dark:hover:text-white"
              >
                Public Site
              </Link>
              <button
                onClick={handleLogout}
                className="text-sm px-4 py-2 border border-gray-300 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800"
              >
                Log out
              </button>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <div className="grid md:grid-cols-2 gap-6 mb-8">
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
            <h2 className="text-lg font-semibold mb-2">API Status</h2>
            {apiHealth?.status === 'ok' ? (
              <div className="flex items-center gap-2 text-green-600 dark:text-green-400">
                <div className="w-2 h-2 bg-green-600 dark:bg-green-400 rounded-full"></div>
                <span>Connected</span>
              </div>
            ) : (
              <div className="flex items-center gap-2 text-red-600 dark:text-red-400">
                <div className="w-2 h-2 bg-red-600 dark:bg-red-400 rounded-full"></div>
                <span>Disconnected</span>
              </div>
            )}
            <div className="mt-4 space-y-2 text-sm text-gray-600 dark:text-gray-400">
              <div>Temporal: {apiHealth?.temporal ? 'Connected' : 'Disconnected'}</div>
              <div className="text-xs">
                API URL: {process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'}
              </div>
            </div>
          </div>

          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
            <h2 className="text-lg font-semibold mb-2">Quick Actions</h2>
            <div className="space-y-2">
              <Link
                href="/studio/episodes/new"
                className="block px-4 py-2 bg-black dark:bg-white text-white dark:text-black rounded-lg text-center font-semibold hover:bg-gray-800 dark:hover:bg-gray-200"
              >
                Start New Episode
              </Link>
              <Link
                href="/studio/episodes/ep04"
                className="block px-4 py-2 border border-gray-300 dark:border-gray-700 rounded-lg text-center hover:bg-gray-50 dark:hover:bg-gray-700"
              >
                View Episode 04
              </Link>
            </div>
          </div>
        </div>

        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h2 className="text-lg font-semibold mb-4">Recent Episodes</h2>
          <div className="text-sm text-gray-600 dark:text-gray-400">
            <p>No episodes found. Start a new episode to begin production.</p>
          </div>
        </div>

        <div className="mt-8 p-6 bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded-lg">
          <h3 className="font-semibold text-yellow-900 dark:text-yellow-200 mb-2">
            ⚠️ Production Safety
          </h3>
          <ul className="text-sm text-yellow-800 dark:text-yellow-300 space-y-1">
            <li>• Dry-run is the default mode - no charges until you switch to live mode</li>
            <li>• Live mode requires G1.08 credit plan approval before any paid generation</li>
            <li>• Budget stops automatically at 80% of each line's cap</li>
            <li>• All gates are fail-closed: missing judge keys escalate to manual review</li>
            <li>• Episode data (beatmaps, continuity) must be uploaded - never committed to public repo</li>
          </ul>
        </div>
      </main>
    </div>
  );
}
