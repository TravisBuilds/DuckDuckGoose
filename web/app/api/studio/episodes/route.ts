import { NextRequest, NextResponse } from 'next/server';
import { cookies } from 'next/headers';

const API_URL = process.env.API_URL || 'http://localhost:8000';
const ADMIN_SECRET = process.env.STUDIO_ADMIN_SECRET || '';

async function getAdminToken(): Promise<string | undefined> {
  const cookieStore = await cookies();
  return cookieStore.get('studio_admin_token')?.value;
}

function verifyToken(token: string | undefined): boolean {
  if (!token || !ADMIN_SECRET) return false;
  return token === ADMIN_SECRET;
}

/**
 * POST /api/studio/episodes - Start a new episode
 */
export async function POST(request: NextRequest) {
  const token = await getAdminToken();
  
  if (!verifyToken(token)) {
    return NextResponse.json(
      { error: 'Unauthorized' },
      { status: 401 }
    );
  }

  try {
    const body = await request.json();
    
    const response = await fetch(`${API_URL}/api/episodes`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${ADMIN_SECRET}`,
      },
      body: JSON.stringify(body),
    });

    if (!response.ok) {
      const error = await response.json();
      return NextResponse.json(error, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: 'Failed to start episode' },
      { status: 500 }
    );
  }
}

/**
 * GET /api/studio/episodes - List episodes
 */
export async function GET(request: NextRequest) {
  const token = await getAdminToken();
  
  if (!verifyToken(token)) {
    return NextResponse.json(
      { error: 'Unauthorized' },
      { status: 401 }
    );
  }

  try {
    const response = await fetch(`${API_URL}/api/episodes`, {
      headers: {
        'Authorization': `Bearer ${ADMIN_SECRET}`,
      },
    });

    if (!response.ok) {
      const error = await response.json();
      return NextResponse.json(error, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: 'Failed to list episodes' },
      { status: 500 }
    );
  }
}
