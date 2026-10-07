import { NextRequest, NextResponse } from 'next/server';

/**
 * Verify session cookie by calling an authenticated backend endpoint.
 * 
 * This route is used by the Next.js frontend to check if the user has a valid
 * session cookie. It proxies to the backend's /api/episodes/{episode_id}/gates
 * endpoint which requires authentication.
 */
export async function POST(request: NextRequest) {
  try {
    const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
    
    // Get the session cookie from the request
    const cookie = request.cookies.get('studio_admin_token');
    
    if (!cookie) {
      return NextResponse.json(
        { error: 'No session cookie' },
        { status: 401 }
      );
    }

    // Test the session cookie against an authenticated backend endpoint
    // Using a simple endpoint that requires authentication but has minimal side effects
    const response = await fetch(`${apiUrl}/api/episodes/ep04/gates`, {
      headers: {
        'Cookie': `studio_admin_token=${cookie.value}`,
      },
      credentials: 'include',
    });

    if (!response.ok) {
      return NextResponse.json(
        { error: 'Invalid or expired session' },
        { status: 401 }
      );
    }

    return NextResponse.json({ success: true });
  } catch (error) {
    return NextResponse.json(
      { error: 'Verification failed' },
      { status: 500 }
    );
  }
}
