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
 * POST /api/studio/episodes/[id]/upload - Upload episode data files
 * 
 * Expects multipart/form-data with:
 * - beatmap: BEATMAP.md file
 * - continuity: CONTINUITY.md file (optional)
 * - creditPlan: CREDIT-PLAN.md file (optional)
 */
export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const token = await getAdminToken();
  
  if (!verifyToken(token)) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const { id } = await params;

  try {
    const formData = await request.formData();
    
    // Forward to backend API
    const response = await fetch(`${API_URL}/api/episodes/${id}/upload`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${ADMIN_SECRET}`,
      },
      body: formData, // Forward multipart as-is
    });

    if (!response.ok) {
      const error = await response.json();
      return NextResponse.json(error, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: 'Failed to upload episode data' },
      { status: 500 }
    );
  }
}
