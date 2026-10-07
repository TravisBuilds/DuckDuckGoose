'use client';

import { useState, Suspense } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';

function LoginForm() {
  const [secret, setSecret] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const router = useRouter();
  const searchParams = useSearchParams();
  const from = searchParams.get('from') || '/studio';

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      // Call the backend /api/login to get a session cookie
      const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
      const response = await fetch(`${apiUrl}/api/login`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ admin_secret: secret }),
        credentials: 'include', // Important: include cookies
      });

      if (!response.ok) {
        throw new Error('Invalid admin secret');
      }

      // Backend sets httpOnly session cookie automatically
      // Do NOT store the raw secret in any cookie or localStorage
      // Use window.location.assign to avoid prefetch cache issues
      window.location.assign(from);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Authentication failed');
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900 px-4">
      <div className="max-w-md w-full">
        <div className="text-center mb-8">
          <h1 className="text-3xl font-bold mb-2">Studio Console</h1>
          <p className="text-gray-600 dark:text-gray-400">
            Enter your admin secret to access the production console
          </p>
        </div>

        <form onSubmit={handleLogin} className="bg-white dark:bg-gray-800 rounded-lg shadow-lg p-8">
          {error && (
            <div className="mb-4 p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded text-sm text-red-800 dark:text-red-200">
              {error}
            </div>
          )}

          <div className="mb-6">
            <label htmlFor="secret" className="block text-sm font-medium mb-2">
              Admin Secret
            </label>
            <input
              type="password"
              id="secret"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              className="w-full px-4 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900 focus:ring-2 focus:ring-black dark:focus:ring-white focus:border-transparent"
              placeholder="Enter your admin secret"
              required
              autoFocus
            />
            <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
              Set in <code className="px-1 py-0.5 bg-gray-100 dark:bg-gray-800 rounded">STUDIO_ADMIN_SECRET</code> environment variable
            </p>
          </div>

          <button
            type="submit"
            disabled={loading || !secret}
            className="w-full px-6 py-3 bg-black dark:bg-white text-white dark:text-black rounded-lg font-semibold hover:bg-gray-800 dark:hover:bg-gray-200 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {loading ? 'Verifying...' : 'Access Console'}
          </button>

          <div className="mt-6 text-center text-sm text-gray-500 dark:text-gray-400">
            <p>Access control: Required for all console and API operations</p>
            <p className="mt-1">Public pages remain accessible without authentication</p>
          </div>
        </form>
      </div>
    </div>
  );
}

export default function StudioLoginPage() {
  return (
    <Suspense fallback={
      <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900">
        <div className="text-center">
          <p className="text-gray-600 dark:text-gray-400">Loading...</p>
        </div>
      </div>
    }>
      <LoginForm />
    </Suspense>
  );
}
