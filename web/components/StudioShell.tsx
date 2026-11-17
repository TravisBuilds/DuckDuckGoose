'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { ApiError, apiFetch } from '@/lib/studio-api';
import { formatUsdMicros } from '@/lib/balance.mjs';
import { roleLabel } from '@/lib/labels';
import { btnPrimary, chip, tones } from '@/lib/ui';
import { Avatar, BottomBar, Icon, IconCircle, LogoMark } from './playful';
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

/**
 * Focus mode (making a Universe): the top bar shows what is being made, whether it is saved and a
 * way out instead of the tabs, and the phone bar steps aside.
 */
export interface StudioFocus {
  label: string;
  name: string;
  saved: 'saved' | 'saving' | 'idle';
  exitHref: string;
}

const FocusContext = createContext<(focus: StudioFocus | null) => void>(() => undefined);

export function useStudioFocus(focus: StudioFocus | null) {
  const setFocus = useContext(FocusContext);
  const key = focus ? `${focus.label}|${focus.name}|${focus.saved}|${focus.exitHref}` : '';
  const latest = useRef(focus);
  latest.current = focus;
  useEffect(() => {
    setFocus(latest.current);
  }, [key, setFocus]);
  useEffect(() => () => setFocus(null), [setFocus]);
}

const NAV: { href: string; label: string; adminOnly?: boolean; active: (path: string) => boolean }[] = [
  { href: '/studio', label: 'Universes', active: (p) => p === '/studio' || p.startsWith('/studio/universes') },
  { href: '/studio/episodes', label: 'Episodes', active: (p) => p.startsWith('/studio/episodes') || p.startsWith('/studio/create') },
  { href: '/dashboard', label: 'Costs', adminOnly: true, active: (p) => p.startsWith('/dashboard') },
  { href: '/studio/users', label: 'Members', adminOnly: true, active: (p) => p.startsWith('/studio/users') },
];

/** The small uppercase title centred in the phone header: which part of the studio this is. */
function sectionTitle(path: string): string {
  if (path === '/studio/universes/new') return 'New Universe';
  if (path.startsWith('/studio/universes/')) return 'Universe';
  if (path === '/studio/episodes') return 'Episodes';
  if (path.startsWith('/studio/episodes/')) return 'Episode';
  if (path.startsWith('/studio/create')) return 'New episode';
  if (path.startsWith('/studio/users')) return 'Members';
  if (path.startsWith('/dashboard')) return 'Costs';
  return path === '/studio' ? 'Universes' : 'Studio';
}

const NEW_ITEMS = [
  { href: '/studio/universes/new', label: 'New Universe', text: 'Characters, places and a look to keep' },
  { href: '/studio/create', label: 'New episode', text: 'A video, in a Universe or on its own' },
];

/** A small menu that closes on Escape and on a click outside it (its other toggles, data-menu=name, excepted). */
function useMenu(name: string) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false);
    const onClick = (e: MouseEvent) => {
      const target = e.target as Element;
      if (ref.current?.contains(target) || target.closest?.(`[data-menu="${name}"]`)) return;
      setOpen(false);
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('mousedown', onClick);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('mousedown', onClick);
    };
  }, [open]);
  return { open, setOpen, ref };
}

function NewMenu() {
  const { open, setOpen, ref } = useMenu('new');
  return (
    <div className="relative hidden sm:block" ref={ref}>
      <button type="button" className={`${btnPrimary} pf-btn-sm`} aria-expanded={open} aria-controls="studio-new-menu"
        onClick={() => setOpen((v) => !v)} data-testid="studio-new-button">
        <Icon name="plus" size={14} />New
      </button>
      {open && (
        <div id="studio-new-menu" className="pf-card absolute right-0 top-[calc(100%+10px)] z-50 w-72 p-2 shadow-float">
          {NEW_ITEMS.map((item) => (
            <Link key={item.href} href={item.href} onClick={() => setOpen(false)}
              className="block rounded-inner px-3 py-2.5 hover:bg-page">
              <span className="block text-[14px] font-extrabold">{item.label}</span>
              <span className="block text-[12.5px] text-ink-soft">{item.text}</span>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * The studio's frame: a white glass pill top bar with the logo, the tabs (Costs and Members only for
 * admins), the admin's balance, "+ New" and who is signed in with their role; on phones a light-blue
 * header with the section's title centred and a menu button, and a white tab bar with the azure
 * "make something" button. Wraps /studio and /dashboard; the marketing header is hidden there
 * (MarketingOnly).
 */
export default function StudioShell({ children }: { children: ReactNode }) {
  const path = usePathname() ?? '';
  const [state, setState] = useState<MeState>({ me: null, loading: true, error: null });
  const [balance, setBalance] = useState<number | null | undefined>(undefined);
  const [focus, setFocusState] = useState<StudioFocus | null>(null);
  const you = useMenu('you');
  const make = useMenu('make');
  const setFocus = useCallback((next: StudioFocus | null) => setFocusState(next), []);

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

  const isAdmin = state.me?.role === 'admin';
  useEffect(() => {
    if (!isAdmin) return undefined;
    let alive = true;
    apiFetch<{ balance_remaining_usd_micros: number | null }>('/api/budget/balance')
      .then((b) => alive && setBalance(b.balance_remaining_usd_micros ?? null))
      .catch(() => alive && setBalance(undefined));
    return () => {
      alive = false;
    };
  }, [isAdmin, path]);

  const closeYou = you.setOpen;
  const closeMake = make.setOpen;
  useEffect(() => {
    closeYou(false);
    closeMake(false);
  }, [path, closeYou, closeMake]);

  const logout = async () => {
    try {
      await apiFetch('/api/logout', { method: 'POST' });
    } catch {
      // the next page asks to sign in again either way
    }
    window.location.assign('/');
  };

  const items = NAV.filter((item) => !item.adminOnly || isAdmin);
  const who = state.me?.name || state.me?.email || '';
  const balanceChip = isAdmin && balance !== undefined && (
    <span className={`${chip} ${balance === null ? tones.warn : tones.ok} hidden !py-1.5 !text-[13px] md:inline-flex`} data-testid="studio-balance">
      {balance === null ? 'Balance not recorded' : `Balance ${formatUsdMicros(balance)}`}
    </span>
  );

  return (
    <MeContext.Provider value={state}>
      <FocusContext.Provider value={setFocus}>
        <div className="flex flex-1 flex-col bg-page">
          <a href="#studio-main"
            className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded-inner focus:bg-white focus:px-3 focus:py-2 focus:text-sm focus:shadow-card">
            Skip to content
          </a>
          <header className="sticky top-0 z-40 bg-sky/95 px-3 pb-1.5 pt-1.5 backdrop-blur-md sm:bg-transparent sm:px-6 sm:pb-0 sm:pt-3.5 sm:backdrop-blur-none lg:px-8"
            data-testid="studio-shell">
            <div className="mx-auto flex max-w-[1240px] items-center gap-2 py-1 sm:gap-4 sm:rounded-full sm:border sm:border-white/95 sm:bg-white/80 sm:py-2 sm:pl-4 sm:pr-2.5 sm:shadow-float sm:backdrop-blur-xl">
              <Link href="/studio" className="flex min-h-11 flex-none items-center gap-2 rounded-full font-display text-[18px] font-bold tracking-[-0.02em]">
                <LogoMark className="h-[34px] w-[34px]" />
                <span className="hidden sm:inline">
                  DuckDuck<span className="text-blue-deep">Goose</span>
                </span>
                <span className="sr-only"> Studio home</span>
              </Link>
              {focus ? (
                <>
                  {/* on a phone the label sits over the name, so the name gets the bar's whole width */}
                  <p className="min-w-0 flex-1 leading-tight sm:flex-initial sm:truncate sm:text-[13px] sm:font-extrabold sm:text-ink-soft"
                    data-testid="studio-focus">
                    <span className="block truncate text-[11.5px] font-extrabold text-ink-soft sm:inline sm:text-[13px]">
                      {focus.label}<span className="hidden sm:inline"> · </span>
                    </span>
                    <span className="block truncate text-[15px] font-extrabold text-ink sm:inline sm:text-[13px]" data-testid="studio-focus-name">
                      {focus.name}
                    </span>
                  </p>
                  <div className="hidden flex-1 sm:block" />
                  <span role="status"
                    className={`${chip} ${focus.saved === 'saving' ? tones.wait : tones.ok} ${focus.saved === 'idle' ? 'invisible max-sm:hidden' : ''} shrink-0 max-sm:!px-2`}
                    data-testid="studio-saved">
                    {focus.saved === 'saving'
                      ? <span aria-hidden="true" className="size-2 rounded-full bg-current motion-safe:animate-pulse sm:hidden" />
                      : <Icon name="check" size={14} className="sm:hidden" />}
                    <span className="max-sm:sr-only">{focus.saved === 'saving' ? 'Saving…' : 'Saved'}</span>
                  </span>
                  {balanceChip}
                  <Link href={focus.exitHref} className="pf-btn pf-btn-ghost pf-btn-sm shrink-0 whitespace-nowrap" data-testid="studio-save-exit">
                    Save &amp; exit
                  </Link>
                </>
              ) : (
                <>
                  <p className="pf-band-title min-w-0 flex-1 truncate sm:hidden" data-testid="studio-section-title">{sectionTitle(path)}</p>
                  <nav aria-label="Studio" className="ml-2 hidden items-center gap-1 sm:flex">
                    {items.map((item) => {
                      const active = item.active(path);
                      return (
                        <Link key={item.href} href={item.href} aria-current={active ? 'page' : undefined}
                          className={`pf-tab ${active ? 'is-on' : ''}`}>
                          {item.label}
                        </Link>
                      );
                    })}
                  </nav>
                  <div className="hidden flex-1 sm:block" />
                  {balanceChip}
                  <NewMenu />
                </>
              )}
              {state.me && (
                <span className={`${chip} ${isAdmin ? tones.info : tones.wait} hidden sm:inline-flex`} data-testid="me-role">
                  {roleLabel(state.me.role)}
                </span>
              )}
              <div className="relative" ref={you.ref}>
                <button type="button" onClick={() => you.setOpen((v) => !v)} aria-expanded={you.open} aria-controls="studio-menu"
                  className="grid size-11 place-items-center rounded-full text-ink max-sm:bg-white/70 max-sm:shadow-card" data-testid="studio-menu-button">
                  <Icon name="menu" size={20} className="sm:hidden" />
                  <Avatar name={who} size={36} className="max-sm:hidden" />
                  <span className="sr-only">{who ? `Signed in as ${who}: open your menu` : 'Open your menu'}</span>
                </button>
                {you.open && (
                  <div id="studio-menu" className="pf-card absolute right-0 top-[calc(100%+10px)] z-50 w-[min(18rem,calc(100vw-1.5rem))] p-3 shadow-float">
                    <div className="flex items-center gap-3 px-2">
                      <Avatar name={who} size={40} />
                      <div className="min-w-0">
                        <p className="break-words text-[14px] font-extrabold" data-testid="me-name">{who || 'Signed in'}</p>
                        {state.me && (
                          <p className="pt-1">
                            <span className={`${chip} ${isAdmin ? tones.info : tones.wait}`}>{roleLabel(state.me.role)}</span>
                          </p>
                        )}
                      </div>
                    </div>
                    <nav aria-label="Your menu" className="mt-2 flex flex-col border-t border-line pt-2 sm:hidden">
                      {items.map((item) => (
                        <Link key={item.href} href={item.href} className="rounded-inner px-2 py-2.5 text-[14px] font-bold hover:bg-page"
                          aria-current={item.active(path) ? 'page' : undefined}>
                          {item.label}
                        </Link>
                      ))}
                    </nav>
                    <button type="button" onClick={logout}
                      className="mt-2 flex w-full items-center gap-2 rounded-inner px-2 py-2.5 text-left text-[14px] font-bold hover:bg-page">
                      <Icon name="exit" size={18} className="text-ink-soft" />
                      Log out
                    </button>
                  </div>
                )}
              </div>
            </div>
          </header>
          {state.error && (
            <p role="alert" className="mx-auto mt-4 w-full max-w-[1240px] px-4 text-sm text-coral-ink sm:px-6 lg:px-8">
              {state.error}
            </p>
          )}
          <main id="studio-main" className={`flex-1 ${focus ? '' : 'pb-28 sm:pb-0'}`}>{children}</main>
          {!focus && (
            <BottomBar label="Studio" testId="studio-bottom-bar"
              items={[
                { key: 'universes', label: 'Universes', href: '/studio', icon: <Icon name="home" size={22} />, active: NAV[0].active(path) },
                { key: 'episodes', label: 'Episodes', href: '/studio/episodes', icon: <Icon name="play" size={22} />, active: NAV[1].active(path) },
                ...(isAdmin ? [{ key: 'costs', label: 'Costs', href: '/dashboard', icon: <Icon name="coins" size={22} />, active: NAV[2].active(path) }] : []),
                {
                  key: 'you', label: 'You', onClick: () => you.setOpen((v) => !v), expanded: you.open, menu: 'you',
                  icon: <Icon name="user" size={22} />,
                },
              ]}
              fab={(
                <div className="relative" ref={make.ref}>
                  <button type="button" className="pf-fab" aria-expanded={make.open} aria-controls="studio-make-menu"
                    onClick={() => make.setOpen((v) => !v)} data-testid="studio-bottom-bar-new">
                    <Icon name="plus" size={24} />
                    <span className="sr-only">Make something new</span>
                  </button>
                  {make.open && (
                    <div id="studio-make-menu" className="pf-card absolute bottom-[calc(100%+18px)] left-1/2 z-50 w-[min(20rem,calc(100vw-1.5rem))] -translate-x-1/2 p-2 shadow-float">
                      {NEW_ITEMS.map((item) => (
                        <Link key={item.href} href={item.href} className="block rounded-inner px-3 py-3 hover:bg-page">
                          <span className="block text-[15px] font-extrabold">{item.label}</span>
                          <span className="block text-[13px] text-ink-soft">{item.text}</span>
                        </Link>
                      ))}
                    </div>
                  )}
                </div>
              )}
            />
          )}
          <ToastHost />
        </div>
      </FocusContext.Provider>
    </MeContext.Provider>
  );
}

/** Admin pages (Members, Costs): everyone else gets a friendly dead end instead of an error. */
export function AdminOnly({ children }: { children: ReactNode }) {
  const { me, loading, error } = useMe();
  if (loading) {
    return <p className="mx-auto max-w-7xl px-4 py-12 text-sm text-ink-soft sm:px-6 lg:px-8">Loading…</p>;
  }
  if (me?.role === 'admin') return <>{children}</>;
  if (error) return null;
  return (
    <div className="mx-auto max-w-xl px-4 py-16 text-center" data-testid="admins-only">
      <IconCircle icon="lock" size={80} className="mx-auto" />
      <h1 className="mt-4 text-[28px]">Only admins can open this page</h1>
      <p className="mt-2 text-ink-soft">
        Your account can make and review videos. Ask an admin if you need something from this page.
      </p>
      <Link href="/studio" className={`${btnPrimary} mt-6`}>Back to the studio</Link>
    </div>
  );
}
