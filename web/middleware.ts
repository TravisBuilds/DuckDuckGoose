import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

/**
 * Middleware to protect studio routes.
 * 
 * Checks for admin secret in cookie. If not present, redirects to login.
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

  // Protect studio routes
  if (pathname.startsWith('/studio')) {
    const adminCookie = request.cookies.get('studio_admin_token');
    
    if (!adminCookie) {
      // Redirect to login
      const url = request.nextUrl.clone();
      url.pathname = '/login';
      url.searchParams.set('from', pathname);
      return NextResponse.redirect(url);
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
