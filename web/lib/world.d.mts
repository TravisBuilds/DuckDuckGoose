import type { UniverseCounts } from './consistency.mjs';
import type { CastCharacter, CastVersion } from './cast.mjs';
import type { PropItem } from './props.mjs';

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
  /** Owners and admins: whether the Universe's story is written (null for everyone else; absent from older APIs). */
  story_written?: boolean | null;
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

export interface UniverseImage {
  image_id: string;
  url: string;
  thumb_url: string;
}

export interface WorldCharacter extends Omit<CastCharacter, 'versions'> {
  versions: (CastVersion & { problems: string[] })[];
  locked_versions: number[];
  primary_image: UniverseImage | null;
  used_in: { episodes: number; versions: Record<string, number> };
  /** The owner's and admins' view only. */
  voice?: SavedVoice | null;
}

export interface WorldItem extends PropItem {
  primary_photo: UniverseImage | null;
  used_in: { episodes: number };
}

export interface WorldEpisode {
  episode_id: string;
  created_at: string | null;
  owner_user_id: string | null;
  owner_name: string | null;
  can_change: boolean;
  finished: boolean;
  primary_file: string | null;
}

export interface WorldNote {
  note_id: string;
  series_id: string;
  topic: string;
  body: string;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface WorldPhoto extends UniverseImage {
  caption: string;
  width: number;
  height: number;
  bytes: number;
  mime: string;
  original_filename: string | null;
  created_at: string;
  size_note?: string | null;
}

export interface WorldBoard {
  board_id: string;
  series_id: string;
  version: number;
  status: 'ready' | 'approved' | 'redo_requested';
  prompt?: string;
  answers_feedback: string;
  redo_feedback: string | null;
  image_count: number;
  estimate_usd_micros: number;
  approved_total_usd_micros: number;
  dry_run: boolean;
  created_at: string;
  images: (UniverseImage & { position: number; width: number; height: number; placeholder: boolean })[];
}

/** The Universe's mood board budget line (U1_mood_board under universe-<series id>). */
export interface BoardBudget {
  line: string;
  cap_usd_micros: number;
  stop_usd_micros: number;
  spent_usd_micros: number;
  reserved_usd_micros: number;
  available_usd_micros: number;
}

export interface BoardEstimate {
  version: number;
  count: number;
  per_image_usd_micros: number[];
  total_usd_micros: number;
  dry_run: boolean;
  redo: boolean;
  summary: string;
  /** Live only: the still model, the reference photos each picture is made with, the budget line. */
  model_path?: string;
  references_per_picture?: number;
  budget?: BoardBudget;
}

/** A live mood board the worker makes picture by picture (no prompt text). */
export interface WorldBoardRequest {
  request_id: string;
  series_id: string;
  status: 'making' | 'done' | 'failed';
  answers_feedback: string;
  replaces_board_id: string | null;
  image_count: number;
  model_path: string;
  estimate_usd_micros: number;
  approved_total_usd_micros: number;
  workflow_id: string;
  board_id: string | null;
  error: string | null;
  requested_by: string | null;
  created_at: string;
  updated_at: string;
  jobs: { position: number; status: string }[];
  made: number;
}

export interface BoardProgress {
  kind: 'making' | 'failed' | 'short';
  made: number;
  asked: number;
  text: string;
}

export declare const BOARD_POLL_MS: number;
export declare function boardProgress(request: WorldBoardRequest | null | undefined): BoardProgress | null;
export declare function boardEstimateLines(estimate: BoardEstimate | null | undefined): string[];

/** The story a Universe starts from (hfvg/story_refs.story_view). */
export interface WorldStory {
  body: string;
  written: boolean;
  updated_at: string | null;
  /** A reference list was made, and made from the story as it reads now. */
  list_made: boolean;
  list_current: boolean;
  list_made_at: string | null;
}

export type ChecklistKind = 'character' | 'place' | 'prop';

/** One reference the story needs (hfvg/story_refs._item_view): photos are reference photo ids. */
export interface ChecklistItem {
  item_id: string;
  kind: ChecklistKind;
  name: string;
  why: string;
  show: string;
  ideal_photos: number;
  min_photos: number;
  optional: boolean;
  source: 'story' | 'owner';
  photos: string[];
  photo_count: number;
  ready: boolean;
}

export interface ChecklistProgress {
  ready: number;
  required: number;
  optional: number;
  optional_ready: number;
  photos: number;
}

export interface WorldChecklist {
  items: ChecklistItem[];
  progress: ChecklistProgress;
}

/** The creation flow (owner and admins): the story and its reference list, references, mood boards,
 * and what the next board may be. ``story`` and ``checklist`` are absent from APIs before stories. */
export interface WorldCreation {
  story?: WorldStory;
  checklist?: WorldChecklist;
  item_kinds?: ChecklistKind[];
  notes: WorldNote[];
  photos: WorldPhoto[];
  boards: WorldBoard[];
  board: { available: boolean; image_count: number; dry_run: boolean; unavailable: string | null };
  board_request: WorldBoardRequest | null;
  topics: string[];
  limits: {
    max_notes: number; note_max: number; max_photos: number; caption_max: number; feedback_max: number;
    story_max?: number; story_min?: number; max_items?: number; max_photos_per_item?: number;
    item_name_max?: number; why_max?: number;
  };
}

/** GET /api/worlds/{id}. */
export interface WorldView {
  world: UniverseCard;
  can_edit: boolean;
  characters: WorldCharacter[];
  items: WorldItem[];
  episodes: WorldEpisode[];
  look: { board_id: string; version: number; images: (UniverseImage & { position: number })[] } | null;
  limits: {
    max_upload_bytes: number;
    min_image_px: number;
    small_image_px?: number;
    sharpen_below_px?: number;
    formats: string[];
    max_uploads_per_version: number;
    max_characters: number;
    max_photos_per_item: number;
    max_items: number;
    max_props_per_shot: number;
    name_max: number;
    description_max: number;
    logline_max: number;
    bible_max: number;
    style_notes_max: number;
  };
  attestation: { rights: string[]; likeness_gate: string };
  creation?: WorldCreation;
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
export declare function isFinished(
  state: { run?: { ended?: boolean | null; outcome?: string | null } | null } | null | undefined,
): boolean;
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

export declare function madeWith(plan: {
  characters?: { slug: string; name: string }[] | null;
  props?: { slug: string; name: string }[] | null;
  shots?: {
    shot_id: string;
    cast?: (string | { character: string; version?: number | null })[] | null;
    place?: string | null;
    props?: string[] | null;
  }[] | null;
} | null | undefined): Record<string, string[]>;

/** POST /api/episodes, as the wizard sends it. */
export interface StartRequest {
  episode_id: string;
  dry_run: boolean;
  enable_story_shaping: boolean;
  archetype_id?: string | null;
  idea?: string;
  target_duration?: number | null;
}

export declare function pendingStartKey(episodeId: string): string;
export declare function startRequest(body: unknown, episodeId: string): StartRequest | null;
export declare function pendingStart(text: string | null | undefined, episodeId: string): StartRequest | null;

/** GET /api/episodes/{id}/universe. */
export interface EpisodeUniverse {
  episode_id: string;
  owner: { user_id: string | null; name: string | null };
  can_act: boolean;
  /** The start the wizard kept for the episode's Start card (only for a caller who may act on it). */
  kept_start?: StartRequest | null;
  /** null: the caller may not see this episode's cast. */
  linked: boolean | null;
  universe: { series_id: string; name: string } | null;
  strip: {
    characters: {
      character_id: string; slug: string; name: string; version: number; exists: boolean; locked: boolean;
      latest_locked: number | null; problems: string[]; image: { url: string; thumb_url: string } | null;
    }[];
    places: StripItem[];
    props: StripItem[];
  } | null;
  lock: { required: boolean; unlocked: { character: string; character_id: string; name: string; version: number }[] } | null;
}

export interface StripItem {
  prop_id: string;
  slug: string;
  kind: string;
  name: string;
  photos: number;
  image: { url: string; thumb_url: string } | null;
}
