/** One picture of a character version (api/cast_routes.py _image_out). */
export interface CastImage {
  image_id: string;
  character_id: string;
  version: number;
  role: string;
  status: string;
  sha256: string;
  ext: string;
  mime: string;
  width: number;
  height: number;
  bytes: number;
  is_primary: boolean;
  placeholder: boolean;
  source: string | null;
  job_id: string | null;
  original_filename: string | null;
  created_by: string | null;
  created_at: string;
  url: string;
  thumb_url: string;
}

export interface SheetJob {
  job_id: string;
  version: number;
  stage: string;
  view: string;
  attempt: number;
  workflow_id: string | null;
  status: string;
  error: string | null;
  image_id: string | null;
  estimated_usd_micros: number | null;
  created_at: string;
}

export interface CastVersion {
  version: number;
  label: string;
  outfit: string;
  derived_from_version: number | null;
  locked: boolean;
  locked_at: string | null;
  lock_hash: string | null;
  sheet_stage: string;
  clause: string;
  images: CastImage[];
  sheet_jobs: SheetJob[];
}

export interface CastCharacter {
  character_id: string;
  slug: string;
  name: string;
  role: string;
  description: string;
  library_kind: string;
  library_id: string;
  default_version: number;
  versions: CastVersion[];
}

export interface CastShot {
  shot_id: string;
  prompt: string;
  characters: string[];
  cast: CastPick[];
  allow_text: boolean | null;
  text_policy: string | null;
  refs: { key: string; label: string }[];
  notes: string[];
}

export interface CastPick {
  character: string;
  version: number | null;
}

export interface SheetEstimate {
  stage: string;
  mode: string;
  model_path: string;
  count: number;
  views: string[];
  per_image_usd_micros: number[];
  total_usd_micros: number;
  resolution: string | null;
  dry_run: boolean;
  summary: string;
}

export interface SeriesInfo {
  series_id: string;
  name: string;
  owner_user_id: string | null;
  episodes?: string[];
  episode_count?: number;
  can_edit?: boolean;
}

/** GET /api/episodes/{id}/cast. */
export interface CastView {
  episode_id: string;
  library: { kind: 'episode' | 'series'; id: string };
  series: SeriesInfo | null;
  characters: CastCharacter[];
  shots: {
    uploaded: boolean;
    shots: CastShot[];
    plan: { errors: string[]; notes: string[]; text_policy: string | null } | null;
    error: string | null;
  };
  still_max_refs: number | null;
  sheet: {
    dry_run: boolean;
    live_mode: boolean;
    model_path: string | null;
    model_error: string | null;
    text_options: [number, number];
    reference_options: [number, number];
  };
  limits: {
    max_upload_bytes: number;
    min_image_px: number;
    formats: string[];
    max_uploads_per_version: number;
    max_characters: number;
    max_cast_per_shot: number;
  };
  can_edit: boolean;
}

export interface RightsAnswers {
  rights: '' | 'original' | 'owned';
  realPerson: boolean;
  likenessConsent: boolean;
}

/** A drafted character in the create wizard (draft-from-idea's `cast`). */
export interface DraftCastEntry {
  code: string;
  slug: string;
  name: string;
  role?: string;
  description?: string;
  shots?: string[];
}

export interface DraftCastRow {
  code: string;
  slug: string;
  name: string;
  role: string;
  description: string;
  shots: string[];
  seriesSlug: string;
}

export interface ImportedCharacter {
  code: string | null;
  slug: string;
  character_id: string;
  name: string;
  default_version: number;
  status: 'created' | 'existing';
}

export type SheetStep = 'start' | 'working' | 'pick' | 'turnaround' | 'approve' | 'locked';

export declare const PICTURE_TYPES: string[];
export declare const PICTURE_ACCEPT: string;
export declare const MAX_CAST_PER_SHOT: number;
export declare const PICTURE_LABELS: Record<string, string>;
export declare const SHEET_ORDER: string[];
export declare const SHEET_STEP_TEXT: Record<SheetStep, string>;

export declare function pictureProblem(
  file: { name?: string; type?: string; size?: number } | null | undefined,
  maxBytes?: number | null,
): string | null;
export declare function emptyRights(): RightsAnswers;
export declare function rightsProblem(answers: RightsAnswers | null | undefined): string | null;
export declare function rightsFields(answers: RightsAnswers): [string, string][];
export declare function versionPictures(version: CastVersion | null | undefined): {
  uploads: CastImage[];
  options: CastImage[];
  sheet: CastImage[];
};
export declare function activeJobs(version: CastVersion | null | undefined): SheetJob[];
export declare function anyActiveJobs(characters: CastCharacter[] | null | undefined): boolean;
export declare function latestRequest(version: CastVersion | null | undefined): SheetJob[];
export declare function jobStatusText(status: string): string;
export declare function requestProblems(version: CastVersion | null | undefined): string[];
export declare function sheetStep(version: CastVersion | null | undefined): SheetStep;
export declare function optionCounts(fromPicture: boolean): number[];
export declare function defaultOptionCount(fromPicture: boolean): number;
export declare function estimateText(estimate: Pick<SheetEstimate, 'count' | 'dry_run' | 'total_usd_micros'>): string;
export declare function sheetHasPlaceholders(version: CastVersion | null | undefined): boolean;

export declare function castPicks(cast: (string | { character: string; version?: number | null })[] | null | undefined): CastPick[];
export declare function castEntries(picks: CastPick[]): (string | { character: string; version: number })[];
export declare function toggleCast(picks: CastPick[], slug: string): CastPick[];
export declare function setCastVersion(picks: CastPick[], slug: string, version: number | null): CastPick[];
export declare function samePicks(a: CastPick[] | null | undefined, b: CastPick[] | null | undefined): boolean;
export declare function shotCastChanges(
  shots: CastShot[],
  picks: Record<string, CastPick[]>,
  allowText: Record<string, boolean | null>,
): { shots: Record<string, (string | { character: string; version: number })[]>; allow_text: Record<string, boolean | null> } | null;

export declare function draftCastRows(cast: DraftCastEntry[] | null | undefined): DraftCastRow[];
export declare function castRowsProblem(rows: DraftCastRow[]): string | null;
export declare function castImportBody(
  rows: DraftCastRow[],
  seriesCharacters?: Pick<CastCharacter, 'slug' | 'name' | 'role' | 'description'>[],
): { characters: { code: string; slug: string; name: string; role: string; description: string }[] };
export declare function shotsWithCast<T extends { shots: { id: string }[] }>(
  shotsJson: T,
  rows: DraftCastRow[],
  imported: Pick<ImportedCharacter, 'code' | 'slug'>[],
): T;
export declare function kitWithCast<T extends { characters?: Record<string, unknown> }>(
  kit: T,
  rows: DraftCastRow[],
  seriesCharacters?: Pick<CastCharacter, 'slug' | 'name' | 'role' | 'description'>[],
): T;
