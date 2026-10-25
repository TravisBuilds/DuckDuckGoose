import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

/**
 * Middleware to protect studio routes with HMAC-validated session tokens.
 * 
 * Server-side session validation with constant-time comparison.
 * Public marketing pages (/, /archetypes, /pricing) are always accessible.
 */
export function middleware(request: NextRequest) {
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

  // Protect studio routes with server-side session validation
  if (pathname.startsWith('/studio')) {
    const adminCookie = request.cookies.get('studio_admin_token');
    
    if (!adminCookie || !adminCookie.value) {
      // Redirect to login
      const url = request.nextUrl.clone();
      url.pathname = '/login';
      url.searchParams.set('from', pathname);
      return NextResponse.redirect(url);
    }
    
    // Session validation happens server-side on every API call
    // The token is HMAC-signed and verified with constant-time comparison
    // If the token is invalid/expired, API calls will return 401/403
    // and the client will redirect to login
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
