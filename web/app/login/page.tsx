'use client';

import { useState, Suspense } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';

function LoginForm() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [secret, setSecret] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [showAdminLogin, setShowAdminLogin] = useState(false);
  const router = useRouter();
  const searchParams = useSearchParams();
  const from = searchParams.get('from') || '/studio';

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
      
      const body = showAdminLogin
        ? { admin_secret: secret }
        : { email, password };

      const response = await fetch(`${apiUrl}/api/login`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(body),
        credentials: 'include',
      });

      if (!response.ok) {
        const data = await response.json().catch(() => ({ detail: 'Authentication failed' }));
        throw new Error(data.detail || 'Authentication failed');
      }

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
            {showAdminLogin ? 'Admin access' : 'Sign in to your account'}
          </p>
        </div>

        <form onSubmit={handleLogin} className="bg-white dark:bg-gray-800 rounded-lg shadow-lg p-8">
          {error && (
            <div className="mb-4 p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded text-sm text-red-800 dark:text-red-200">
              {error}
            </div>
          )}

          {!showAdminLogin ? (
            <>
              <div className="mb-4">
                <label htmlFor="email" className="block text-sm font-medium mb-2">
                  Email
                </label>
                <input
                  type="email"
                  id="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className="w-full px-4 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900 focus:ring-2 focus:ring-black dark:focus:ring-white focus:border-transparent"
                  placeholder="you@example.com"
                  required
                  autoFocus
                />
              </div>

              <div className="mb-6">
                <label htmlFor="password" className="block text-sm font-medium mb-2">
                  Password
                </label>
                <input
                  type="password"
                  id="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="w-full px-4 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900 focus:ring-2 focus:ring-black dark:focus:ring-white focus:border-transparent"
                  placeholder="Enter your password"
                  required
                />
              </div>
            </>
          ) : (
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
                placeholder="Enter admin secret"
                required
                autoFocus
              />
              <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                Set in <code className="px-1 py-0.5 bg-gray-100 dark:bg-gray-800 rounded">STUDIO_ADMIN_SECRET</code> environment variable
              </p>
            </div>
          )}

          <button
            type="submit"
            disabled={loading || (!showAdminLogin && (!email || !password)) || (showAdminLogin && !secret)}
            className="w-full px-6 py-3 bg-black dark:bg-white text-white dark:text-black rounded-lg font-semibold hover:bg-gray-800 dark:hover:bg-gray-200 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {loading ? 'Signing in...' : 'Sign In'}
          </button>

          <div className="mt-6 text-center">
            <button
              type="button"
              onClick={() => {
                setShowAdminLogin(!showAdminLogin);
                setError('');
                setEmail('');
                setPassword('');
                setSecret('');
              }}
              className="text-sm text-gray-600 dark:text-gray-400 hover:text-black dark:hover:text-white"
            >
              {showAdminLogin ? '← Back to email sign in' : 'Sign in with admin secret'}
            </button>
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
