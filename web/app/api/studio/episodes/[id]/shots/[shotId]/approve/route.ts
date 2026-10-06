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

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string; shotId: string }> }
) {
  const token = getAdminToken();
  
  if (!verifyToken(token)) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const { id, shotId } = await params;

  try {
    const body = await request.json();
    
    const response = await fetch(
      `${API_URL}/api/episodes/${id}/shots/${shotId}/approve`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${ADMIN_SECRET}`,
        },
        body: JSON.stringify(body),
      }
    );

    if (!response.ok) {
      const error = await response.json();
      return NextResponse.json(error, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: 'Failed to approve shot' },
      { status: 500 }
    );
  }
}
