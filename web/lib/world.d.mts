import type { UniverseCounts } from './consistency.mjs';

/** One card of GET /api/worlds. */
export interface UniverseCard {
  series_id: string;
  name: string;
  owner_user_id: string | null;
  owner_name: string | null;
  created_at: string | null;
  can_edit: boolean;
  profile: {
    logline: string;
    preset_id: string | null;
    cover_image_id: string | null;
    updated_at: string | null;
    bible?: string;
    style_notes?: string;
    narrator_voice?: SavedVoice | null;
    bible_board_id?: string | null;
    bible_generated_at?: string | null;
  };
  counts: Required<UniverseCounts>;
  cover: { kind: string; image_id: string; url: string; thumb_url: string } | null;
  episodes: string[];
  board_status: string | null;
}

/** A voice saved on a Universe character or as its narrator (hfvg/world_store._voice_view). */
export interface SavedVoice {
  voice_id: string;
  name: string;
  description: string;
  preview_url: string | null;
  chosen_by?: string | null;
  chosen_at?: string | null;
}

export interface HomeEpisode {
  episode_id: string;
  user_id: string | null;
  owner_name?: string | null;
  created_at?: string | null;
}

export interface HomeViewer {
  userId: string | null;
  isAdmin: boolean;
}

export interface HomeEntry<E extends HomeEpisode, U extends { series_id: string; episodes?: string[] }> {
  episode: E;
  universe: U | null;
}

export declare function universeByEpisode<U extends { episodes?: string[] }>(worlds: U[] | null | undefined): Map<string, U>;
export declare function mayChange(episode: HomeEpisode | null | undefined, viewer: HomeViewer | null | undefined): boolean;
export declare function isFinished(state: { run?: { ended?: boolean; outcome?: string | null } | null } | null | undefined): boolean;
export declare function homeSections<E extends HomeEpisode, U extends { series_id: string; episodes?: string[] }>(
  worlds: U[] | null | undefined,
  episodes: E[] | null | undefined,
  finished: Set<string> | string[] | null | undefined,
  viewer: HomeViewer | null | undefined,
): { continue: HomeEntry<E, U>[]; finished: HomeEntry<E, U>[]; unfiled: HomeEntry<E, U>[] };
export declare function isFirstRun(
  worlds: unknown[] | null | undefined,
  episodes: HomeEpisode[] | null | undefined,
  viewer: HomeViewer | null | undefined,
): boolean;
export declare function universeChoices(
  worlds: { series_id: string; name: string; can_edit: boolean }[] | null | undefined,
): { series_id: string; name: string }[];
export declare function primaryVideo(files: Record<string, { available: boolean }> | null | undefined): string | null;
export declare function posterShot(
  shotOutcomes: Record<string, { still_url?: string | null }> | null | undefined,
): string | null;
