'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { ApiError, apiFetch } from '@/lib/studio-api';
import { roleLabel } from '@/lib/labels';
import { btnPrimary, chip, focusRing, tones } from '@/lib/ui';
import { ToastHost } from './Toast';

export interface Me {
  user_id: string | null;
  name: string | null;
  email: string | null;
  role: string;
}

interface MeState {
  me: Me | null;
  loading: boolean;
  error: string | null;
}

const MeContext = createContext<MeState>({ me: null, loading: true, error: null });

/** Who is signed in (GET /api/me), for role-aware pages inside the studio shell. */
export function useMe(): MeState & { isAdmin: boolean } {
  const state = useContext(MeContext);
  return { ...state, isAdmin: state.me?.role === 'admin' };
}

const NAV: { href: string; label: string; adminOnly?: boolean; active: (path: string) => boolean }[] = [
  {
    href: '/studio', label: 'Universes',
    active: (p) => p === '/studio' || p.startsWith('/studio/universes') || p.startsWith('/studio/episodes'),
  },
  { href: '/studio/create', label: 'New episode', active: (p) => p.startsWith('/studio/create') },
  { href: '/studio/users', label: 'Members', adminOnly: true, active: (p) => p.startsWith('/studio/users') },
  { href: '/dashboard', label: 'Costs', adminOnly: true, active: (p) => p.startsWith('/dashboard') },
];

function NavLinks({ items, path, onNavigate, vertical }: {
  items: typeof NAV; path: string; onNavigate?: () => void; vertical?: boolean;
}) {
  return (
    <>
      {items.map((item) => {
        const active = item.active(path);
        return (
          <Link key={item.href} href={item.href} onClick={onNavigate}
            aria-current={active ? 'page' : undefined}
            className={`rounded-md px-3 py-2 text-sm font-medium ${focusRing} ${vertical ? 'block' : ''} ${
              active
                ? 'bg-gray-100 text-gray-900 dark:bg-gray-700 dark:text-white'
                : 'text-gray-700 hover:bg-gray-50 hover:text-gray-900 dark:text-gray-200 dark:hover:bg-gray-700'
            }`}>
            {item.label}
          </Link>
        );
      })}
    </>
  );
}

function RoleChip({ role, testId }: { role: string; testId?: string }) {
  return <span className={`${chip} ${role === 'admin' ? tones.todo : tones.wait}`} data-testid={testId}>{roleLabel(role)}</span>;
}

/**
 * The studio's frame: logo, navigation (admin links only for admins), who is signed in with their
 * role, Log out, and a menu button on phones. Wraps /studio and /dashboard; the marketing header is
 * hidden there (MarketingOnly).
 */
export default function StudioShell({ children }: { children: ReactNode }) {
  const path = usePathname() ?? '';
  const [state, setState] = useState<MeState>({ me: null, loading: true, error: null });
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    let alive = true;
    apiFetch<Me>('/api/me')
      .then((me) => alive && setState({ me, loading: false, error: null }))
      .catch((e) => {
        if (!alive) return;
        if (e instanceof ApiError && e.status === 401) {
          window.location.assign(`/login?from=${encodeURIComponent(window.location.pathname + window.location.search)}`);
          return;
        }
        setState({
          me: null,
          loading: false,
          error: e instanceof ApiError ? e.message : 'The studio could not be reached. Check your connection and reload.',
        });
      });
    return () => {
      alive = false;
    };
  }, []);

  const logout = async () => {
    try {
      await apiFetch('/api/logout', { method: 'POST' });
    } catch {
      // the next page asks to sign in again either way
    }
    window.location.assign('/');
  };

  const items = NAV.filter((item) => !item.adminOnly || state.me?.role === 'admin');
  const who = state.me?.name || state.me?.email || '';

  return (
    <MeContext.Provider value={state}>
      <div className="flex flex-1 flex-col bg-gray-50 dark:bg-gray-900">
        <a href="#studio-main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-white focus:px-3 focus:py-2 focus:text-sm focus:shadow">
          Skip to content
        </a>
        <header className="border-b border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800" data-testid="studio-shell">
          <div className="mx-auto flex h-14 max-w-7xl items-center gap-3 px-4 sm:px-6 lg:px-8">
            <Link href="/studio" className={`mr-2 rounded text-lg font-bold text-gray-900 dark:text-white ${focusRing}`}>
              DuckDuckGoose <span className="font-normal text-gray-600 dark:text-gray-300">Studio</span>
            </Link>
            <nav aria-label="Studio" className="hidden items-center gap-1 md:flex">
              <NavLinks items={items} path={path} />
            </nav>
            <div className="ml-auto hidden items-center gap-3 md:flex">
              {who && <span className="text-sm text-gray-700 dark:text-gray-200" data-testid="me-name">{who}</span>}
              {state.me && <RoleChip role={state.me.role} testId="me-role" />}
              <button type="button" onClick={logout}
                className={`rounded-md px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 dark:text-gray-200 dark:hover:bg-gray-700 ${focusRing}`}>
                Log out
              </button>
            </div>
            <button type="button" className={`ml-auto rounded-md p-2 text-gray-700 hover:bg-gray-100 dark:text-gray-200 dark:hover:bg-gray-700 md:hidden ${focusRing}`}
              aria-expanded={menuOpen} aria-controls="studio-menu" onClick={() => setMenuOpen((open) => !open)}
              data-testid="studio-menu-button">
              <span className="sr-only">{menuOpen ? 'Close menu' : 'Open menu'}</span>
              <svg aria-hidden="true" viewBox="0 0 24 24" className="h-6 w-6" fill="none" stroke="currentColor" strokeWidth="2">
                {menuOpen
                  ? <path strokeLinecap="round" d="M6 6l12 12M18 6L6 18" />
                  : <path strokeLinecap="round" d="M4 7h16M4 12h16M4 17h16" />}
              </svg>
            </button>
          </div>
          {menuOpen && (
            <div id="studio-menu" className="border-t border-gray-200 px-4 pb-4 pt-2 dark:border-gray-700 md:hidden">
              <nav aria-label="Studio menu" className="flex flex-col gap-1">
                <NavLinks items={items} path={path} onNavigate={() => setMenuOpen(false)} vertical />
              </nav>
              <div className="mt-3 flex items-center gap-3 border-t border-gray-200 pt-3 dark:border-gray-700">
                {who && <span className="text-sm text-gray-700 dark:text-gray-200">{who}</span>}
                {state.me && <RoleChip role={state.me.role} />}
                <button type="button" onClick={logout}
                  className={`ml-auto rounded-md px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 dark:text-gray-200 dark:hover:bg-gray-700 ${focusRing}`}>
                  Log out
                </button>
              </div>
            </div>
          )}
        </header>
        {state.error && (
          <p role="alert" className="mx-auto mt-4 w-full max-w-7xl px-4 text-sm text-red-700 dark:text-red-300 sm:px-6 lg:px-8">
            {state.error}
          </p>
        )}
        <main id="studio-main" className="flex-1">{children}</main>
        <ToastHost />
      </div>
    </MeContext.Provider>
  );
}

/** Admin pages (Members, Costs): everyone else gets a friendly dead end instead of an error. */
export function AdminOnly({ children }: { children: ReactNode }) {
  const { me, loading, error } = useMe();
  if (loading) {
    return <p className="mx-auto max-w-7xl px-4 py-12 text-sm text-gray-600 dark:text-gray-300 sm:px-6 lg:px-8">Loading…</p>;
  }
  if (me?.role === 'admin') return <>{children}</>;
  if (error) return null;
  return (
    <div className="mx-auto max-w-xl px-4 py-16 text-center" data-testid="admins-only">
      <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Only admins can open this page</h1>
      <p className="mt-2 text-gray-600 dark:text-gray-300">
        Your account can make and review videos. Ask an admin if you need something from this page.
      </p>
      <Link href="/studio" className={`${btnPrimary} mt-6`}>Back to the studio</Link>
    </div>
  );
}
