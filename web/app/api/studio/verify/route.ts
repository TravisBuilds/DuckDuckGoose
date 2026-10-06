import { NextRequest, NextResponse } from 'next/server';

/**
 * Verify admin secret by testing against the backend API.
 */
export async function POST(request: NextRequest) {
  try {
    const { secret } = await request.json();

    if (!secret) {
      return NextResponse.json(
        { error: 'Admin secret required' },
        { status: 400 }
      );
    }

    // Test the secret against the backend API
    const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
    
    const response = await fetch(`${apiUrl}/api/health`, {
      headers: {
        'Authorization': `Bearer ${secret}`,
      },
    });

    if (!response.ok) {
      return NextResponse.json(
        { error: 'Invalid admin secret' },
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
