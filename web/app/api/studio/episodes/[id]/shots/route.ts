import { NextRequest, NextResponse } from 'next/server';
import { cookies } from 'next/headers';

const API_URL = process.env.API_URL || 'http://localhost:8000';
const ADMIN_SECRET = process.env.STUDIO_ADMIN_SECRET || '';

function getAdminToken(): string | undefined {
  const cookieStore = cookies();
  return cookieStore.get('studio_admin_token')?.value;
}

function verifyToken(token: string | undefined): boolean {
  if (!token || !ADMIN_SECRET) return false;
  return token === ADMIN_SECRET;
}

/**
 * GET /api/studio/episodes/[id]/shots - Get all shots for episode with status
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const token = getAdminToken();
  
  if (!verifyToken(token)) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const { id } = await params;

  try {
    const response = await fetch(`${API_URL}/api/episodes/${id}/shots`, {
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
      { error: 'Failed to get shots' },
      { status: 500 }
    );
  }
}
