import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';
import { sessionKeyFromEnv, verifySessionToken } from './lib/session.mjs';

/**
 * Protect studio routes (G-AUTH-1): the studio_admin_token cookie must be a session token the API
 * signed (HMAC-SHA256, constant-time compare) and not expired. Verified here with Web Crypto
 * (Edge-runtime compatible, lib/session.mjs) using the same SESSION_SECRET / ADMIN_SECRET as the API.
 * A missing, forged or expired cookie, or a server without a configured key, redirects to /login
 * (fail closed). The API additionally checks its session table, so logout revokes immediately.
 * Public marketing pages (/, /archetypes, /pricing) are always accessible.
 */
export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Allow public marketing pages and login
  if (
    pathname === '/' ||
    pathname === '/login' ||
    pathname.startsWith('/archetypes') ||
    pathname.startsWith('/pricing') ||
    pathname.startsWith('/_next') ||
    pathname.startsWith('/api')
  ) {
    return NextResponse.next();
  }

  if (pathname.startsWith('/studio')) {
    const token = request.cookies.get('studio_admin_token')?.value;
    const key = await sessionKeyFromEnv(process.env);
    const verdict = await verifySessionToken(token, key);
    if (!verdict.ok) {
      const url = request.nextUrl.clone();
      url.pathname = '/login';
      url.search = '';
      url.searchParams.set('from', pathname);
      if (token) url.searchParams.set('reason', verdict.reason === 'expired' ? 'expired' : 'invalid');
      if (!key) console.error('studio middleware: SESSION_SECRET / ADMIN_SECRET not configured; refusing all sessions');
      const response = NextResponse.redirect(url);
      if (token) response.cookies.delete('studio_admin_token');
      return response;
    }
  }

  return NextResponse.next();
}

export const config = {
  matcher: [
    /*
     * Match all request paths except:
     * - _next/static (static files)
     * - _next/image (image optimization files)
     * - favicon.ico (favicon file)
     */
    '/((?!_next/static|_next/image|favicon.ico).*)',
  ],
};
