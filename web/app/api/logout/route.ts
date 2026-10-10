import { NextRequest, NextResponse } from 'next/server';

/**
 * Logout route - clears the session cookie by calling backend and client-side.
 */
export async function POST(request: NextRequest) {
  try {
    const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
    
    // Call backend logout endpoint (if it exists) to invalidate session server-side
    try {
      const cookie = request.cookies.get('studio_admin_token');
      if (cookie) {
        await fetch(`${apiUrl}/api/logout`, {
          method: 'POST',
          headers: {
            'Cookie': `studio_admin_token=${cookie.value}`,
          },
          credentials: 'include',
        });
      }
    } catch {
      // Backend logout failed or doesn't exist - still clear client cookie
    }

    // Clear the cookie on the client side
    const response = NextResponse.json({ success: true });
    response.cookies.delete('studio_admin_token');
    
    return response;
  } catch (error) {
    return NextResponse.json(
      { error: 'Logout failed' },
      { status: 500 }
    );
  }
}
