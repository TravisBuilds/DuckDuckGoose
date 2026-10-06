/**
 * Studio API client for the production console.
 * 
 * Reads API URL and admin secret from environment variables.
 */

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
const ADMIN_SECRET = process.env.STUDIO_ADMIN_SECRET || '';

if (!ADMIN_SECRET && typeof window === 'undefined') {
  console.warn('STUDIO_ADMIN_SECRET not set - studio console will not work');
}

class APIClient {
  private baseURL: string;
  private adminSecret: string;

  constructor(baseURL: string = API_URL, adminSecret: string = ADMIN_SECRET) {
    this.baseURL = baseURL;
    this.adminSecret = adminSecret;
  }

  private async fetch(endpoint: string, options: RequestInit = {}) {
    const url = `${this.baseURL}${endpoint}`;
    
    const headers: HeadersInit = {
      'Content-Type': 'application/json',
      ...options.headers,
    };

    // Add authorization if admin secret is set
    if (this.adminSecret) {
      headers['Authorization'] = `Bearer ${this.adminSecret}`;
    }

    const response = await fetch(url, {
      ...options,
      headers,
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({
        detail: `HTTP ${response.status}: ${response.statusText}`,
      }));
      throw new Error(error.detail || 'API request failed');
    }

    return response.json();
  }

  async health() {
    return this.fetch('/api/health');
  }

  async startEpisode(episodeId: string, beatmapContent?: string, dryRun: boolean = true) {
    return this.fetch('/api/episodes', {
      method: 'POST',
      body: JSON.stringify({
        episode_id: episodeId,
        beatmap_content: beatmapContent,
        dry_run: dryRun,
      }),
    });
  }

  async getEpisodeState(episodeId: string) {
    return this.fetch(`/api/episodes/${episodeId}`);
  }

  async approveGate(episodeId: string, gateId: string, note?: string) {
    return this.fetch(`/api/episodes/${episodeId}/approve`, {
      method: 'POST',
      body: JSON.stringify({
        gate_id: gateId,
        note,
      }),
    });
  }

  async getBudgetStatus(episodeId: string) {
    return this.fetch(`/api/episodes/${episodeId}/budget`);
  }

  async getShots(episodeId: string) {
    return this.fetch(`/api/episodes/${episodeId}/shots`);
  }

  async setLiveMode(episodeId: string) {
    return this.fetch(`/api/episodes/${episodeId}/set-live`, {
      method: 'POST',
    });
  }
}

export const apiClient = new APIClient();

export type { APIClient };
