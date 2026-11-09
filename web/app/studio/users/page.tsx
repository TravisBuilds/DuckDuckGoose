'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';

interface User {
  user_id: string;
  name: string;
  email: string;
  role: string;
  created_at: string;
  last_active?: string;
}

export default function UsersPage() {
  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [createForm, setCreateForm] = useState({
    name: '',
    email: '',
    role: 'member',
    useInvite: true,
  });
  const [createLoading, setCreateLoading] = useState(false);
  const [inviteUrl, setInviteUrl] = useState('');
  const router = useRouter();

  useEffect(() => {
    fetchUsers();
  }, []);

  const fetchUsers = async () => {
    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
      const response = await fetch(`${apiUrl}/api/users`, {
        credentials: 'include',
      });

      if (response.status === 401 || response.status === 403) {
        router.push('/login?from=/studio/users');
        return;
      }

      if (!response.ok) {
        throw new Error('Failed to fetch users');
      }

      const data = await response.json();
      setUsers(data.users || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load users');
    } finally {
      setLoading(false);
    }
  };

  const handleCreateUser = async (e: React.FormEvent) => {
    e.preventDefault();
    setCreateLoading(true);
    setError('');
    setInviteUrl('');

    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
      const body: any = {
        name: createForm.name,
        email: createForm.email,
        role: createForm.role,
      };

      // Don't include password to generate invite link
      if (!createForm.useInvite) {
        body.password = prompt('Enter initial password (min 8 chars):');
        if (!body.password || body.password.length < 8) {
          setError('Password must be at least 8 characters');
          setCreateLoading(false);
          return;
        }
      }

      const response = await fetch(`${apiUrl}/api/users`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(body),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || 'Failed to create user');
      }

      const data = await response.json();
      
      if (data.invite_url) {
        setInviteUrl(data.invite_url);
      }

      // Refresh user list
      await fetchUsers();
      
      if (!data.invite_url) {
        // Close form if no invite link to show
        setShowCreateForm(false);
        setCreateForm({ name: '', email: '', role: 'member', useInvite: true });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create user');
    } finally {
      setCreateLoading(false);
    }
  };

  const handleGenerateResetLink = async (userId: string) => {
    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
      const response = await fetch(`${apiUrl}/api/users/${userId}/reset-password`, {
        method: 'POST',
        credentials: 'include',
      });

      if (!response.ok) {
        throw new Error('Failed to generate reset link');
      }

      const data = await response.json();
      
      // Copy to clipboard
      await navigator.clipboard.writeText(data.reset_url);
      alert('Password reset link copied to clipboard!\nValid for 72 hours.');
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to generate reset link');
    }
  };

  const copyInviteLink = async () => {
    try {
      await navigator.clipboard.writeText(inviteUrl);
      alert('Invite link copied to clipboard!');
    } catch (err) {
      alert('Failed to copy to clipboard');
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-center">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-black dark:border-white mx-auto mb-4"></div>
          <p className="text-gray-600 dark:text-gray-400">Loading users...</p>
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
              <h1 className="text-2xl font-bold">User Management</h1>
              <p className="text-sm text-gray-600 dark:text-gray-400">
                Manage studio users and permissions
              </p>
            </div>
            <Link
              href="/studio"
              className="text-sm text-gray-600 dark:text-gray-400 hover:text-black dark:hover:text-white"
            >
              ← Back to Studio
            </Link>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {error && (
          <div className="mb-6 p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg text-red-800 dark:text-red-200">
            {error}
          </div>
        )}

        <div className="mb-6">
          <button
            onClick={() => setShowCreateForm(!showCreateForm)}
            className="px-6 py-3 bg-black dark:bg-white text-white dark:text-black rounded-lg font-semibold hover:bg-gray-800 dark:hover:bg-gray-200"
          >
            {showCreateForm ? 'Cancel' : 'Create User'}
          </button>
        </div>

        {showCreateForm && (
          <div className="mb-8 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
            <h2 className="text-lg font-semibold mb-4">Create New User</h2>
            
            {inviteUrl ? (
              <div className="space-y-4">
                <div className="p-4 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-lg">
                  <p className="text-sm font-medium text-green-800 dark:text-green-200 mb-2">
                    ✓ User created successfully!
                  </p>
                  <p className="text-sm text-green-700 dark:text-green-300 mb-4">
                    Share this invite link with the user (valid for 72 hours):
                  </p>
                  <div className="flex gap-2">
                    <input
                      type="text"
                      value={inviteUrl}
                      readOnly
                      className="flex-1 px-3 py-2 text-sm border border-green-300 dark:border-green-700 rounded bg-white dark:bg-gray-900 font-mono"
                    />
                    <button
                      onClick={copyInviteLink}
                      className="px-4 py-2 bg-green-600 text-white rounded hover:bg-green-700 text-sm font-medium"
                    >
                      Copy Link
                    </button>
                  </div>
                </div>
                <button
                  onClick={() => {
                    setShowCreateForm(false);
                    setInviteUrl('');
                    setCreateForm({ name: '', email: '', role: 'member', useInvite: true });
                  }}
                  className="px-4 py-2 bg-gray-200 dark:bg-gray-700 rounded hover:bg-gray-300 dark:hover:bg-gray-600"
                >
                  Close
                </button>
              </div>
            ) : (
              <form onSubmit={handleCreateUser} className="space-y-4">
                <div>
                  <label className="block text-sm font-medium mb-2">Name</label>
                  <input
                    type="text"
                    value={createForm.name}
                    onChange={(e) => setCreateForm({ ...createForm, name: e.target.value })}
                    className="w-full px-4 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900"
                    required
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium mb-2">Email</label>
                  <input
                    type="email"
                    value={createForm.email}
                    onChange={(e) => setCreateForm({ ...createForm, email: e.target.value })}
                    className="w-full px-4 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900"
                    required
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium mb-2">Role</label>
                  <select
                    value={createForm.role}
                    onChange={(e) => setCreateForm({ ...createForm, role: e.target.value })}
                    className="w-full px-4 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900"
                  >
                    <option value="member">Member</option>
                    <option value="admin">Admin</option>
                  </select>
                  <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                    Admin: Full access. Member: Episode management only.
                  </p>
                </div>

                <div className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    id="useInvite"
                    checked={createForm.useInvite}
                    onChange={(e) => setCreateForm({ ...createForm, useInvite: e.target.checked })}
                    className="rounded"
                  />
                  <label htmlFor="useInvite" className="text-sm">
                    Generate invite link (recommended - user sets own password)
                  </label>
                </div>

                <div className="flex gap-2">
                  <button
                    type="submit"
                    disabled={createLoading}
                    className="px-6 py-2 bg-black dark:bg-white text-white dark:text-black rounded-lg font-semibold hover:bg-gray-800 dark:hover:bg-gray-200 disabled:opacity-50"
                  >
                    {createLoading ? 'Creating...' : 'Create User'}
                  </button>
                  <button
                    type="button"
                    onClick={() => setShowCreateForm(false)}
                    className="px-6 py-2 bg-gray-200 dark:bg-gray-700 rounded-lg hover:bg-gray-300 dark:hover:bg-gray-600"
                  >
                    Cancel
                  </button>
                </div>
              </form>
            )}
          </div>
        )}

        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
          <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
            <h2 className="text-lg font-semibold">Users ({users.length})</h2>
          </div>
          
          <div className="divide-y divide-gray-200 dark:divide-gray-700">
            {users.map((user) => (
              <div key={user.user_id} className="px-6 py-4 hover:bg-gray-50 dark:hover:bg-gray-800/50">
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <div className="flex items-center gap-3">
                      <h3 className="font-medium">{user.name}</h3>
                      <span className={`px-2 py-1 text-xs font-medium rounded ${
                        user.role === 'admin'
                          ? 'bg-purple-100 dark:bg-purple-900/20 text-purple-800 dark:text-purple-200'
                          : 'bg-blue-100 dark:bg-blue-900/20 text-blue-800 dark:text-blue-200'
                      }`}>
                        {user.role}
                      </span>
                    </div>
                    <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">
                      {user.email}
                    </p>
                    <p className="text-xs text-gray-500 dark:text-gray-500 mt-1">
                      Created: {new Date(user.created_at).toLocaleDateString()}
                      {user.last_active && ` · Last active: ${new Date(user.last_active).toLocaleDateString()}`}
                    </p>
                  </div>
                  
                  <button
                    onClick={() => handleGenerateResetLink(user.user_id)}
                    className="ml-4 px-3 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded hover:bg-gray-50 dark:hover:bg-gray-700"
                  >
                    Copy Reset Link
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      </main>
    </div>
  );
}
