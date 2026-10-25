/** Browser-side helpers for the studio pages: every call sends the httpOnly session cookie. */
export const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function detailText(body: unknown, fallback: string): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === 'string') return detail;
    return JSON.stringify(detail);
  }
  return fallback;
}

/** fetch + JSON; non-2xx raises ApiError carrying the API's `detail` (never swallowed). */
export async function apiFetch<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, { credentials: 'include', ...init });
  const body = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, detailText(body, `${res.status} ${res.statusText}`));
  return body as T;
}

export function postJson<T = unknown>(path: string, payload: unknown): Promise<T> {
  return apiFetch<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

/** Same id format as the API (api/main.py EPISODE_ID_PATTERN). */
export const EPISODE_ID_RE = /^ep[0-9]{2}$/;
