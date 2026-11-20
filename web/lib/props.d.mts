import type { RightsAnswers } from './cast.mjs';

export type PropKind = 'prop' | 'place';

/** One photo of a prop or place (api/prop_routes.py _photo_out). */
export interface PropPhoto {
  image_id: string;
  prop_id: string;
  status: string;
  sha256: string;
  ext: string;
  mime: string;
  width: number;
  height: number;
  bytes: number;
  is_primary: boolean;
  original_filename: string | null;
  created_by: string | null;
  created_at: string;
  url: string;
  thumb_url: string;
  size_note?: string | null;
}

export interface PropItem {
  prop_id: string;
  slug: string;
  kind: PropKind;
  name: string;
  description: string;
  library_kind: string;
  library_id: string;
  photos: PropPhoto[];
}

/** GET /api/episodes/{id}/props. */
export interface PropsView {
  episode_id: string;
  library: { kind: string; id: string };
  series: { series_id: string; name: string; episodes?: string[] } | null;
  items: PropItem[];
  limits: {
    max_upload_bytes: number;
    min_image_px: number;
    small_image_px?: number;
    sharpen_below_px?: number;
    formats: string[];
    max_photos_per_item: number;
    max_items: number;
    max_props_per_shot: number;
    name_max: number;
    description_max: number;
  };
  can_edit: boolean;
}

export type CastEntry = string | { character: string; version?: number | null };

export interface ShotPlanEdit {
  id: number;
  shot_id: string;
  field: string;
  old: unknown;
  new: unknown;
  actor: string | null;
  actor_name: string | null;
  run_id: string | null;
  note: string | null;
  at: string;
}

export interface ShotPlanShot {
  shot_id: string;
  prompt: string | null; // null (with end_prompt and clip_prompt) for a member who may not see this episode's prompts (prompts_hidden)
  end_prompt: string | null;
  clip_prompt: string | null;
  end_frame: boolean;
  model: string | null;
  duration: number | null;
  cast: CastEntry[] | null;
  props: string[] | null;
  place: string | null;
  refs: string[];
  notes: string[];
  errors: string[];
  history: ShotPlanEdit[];
}

/** GET /api/episodes/{id}/shot-plan. */
export interface ShotPlanView {
  episode_id: string;
  library: { kind: string; id: string };
  phase: { state: 'open' | 'g1' | 'locked'; run_id: string | null; reason: string | null; replanning: boolean };
  may_change: boolean;
  prompts_hidden: boolean;
  can_edit: boolean;
  uploaded: boolean;
  characters: { slug: string; name: string; role: string | null; thumb_url: string | null }[];
  props: { prop_id: string; slug: string; kind: PropKind; name: string; description: string; photos: number;
    thumb_url: string | null }[];
  shots: ShotPlanShot[];
  errors: string[];
  notes: string[];
  limits: { max_cast_per_shot: number; max_props_per_shot: number; prompt_max: number; note_max: number };
}

export interface ShotDraft {
  prompt: string;
  end_prompt: string;
  clip_prompt: string;
  cast: CastEntry[];
  props: string[];
  place: string;
}

export type ShotChanges = Partial<{ prompt: string; end_prompt: string; clip_prompt: string; cast: CastEntry[];
  props: string[]; place: string }>;

export interface ShotPlanSaveResponse extends ShotPlanView {
  changed: { shot_id: string; field: string }[];
  result: { success: boolean; changed: Record<string, string[]>; preflight_errors: string[] } | null;
}

export const MAX_PROPS_PER_SHOT: number;
export const KINDS: PropKind[];
export const KIND_TEXT: Record<PropKind, { label: string; choice: string; hint: string }>;
export const FIELD_TEXT: Record<string, string>;
export function propAddForm(view: PropsView | null | undefined): { shown: boolean; first: boolean; title: string };
export function addPropProblem(kind: string, name: string, file: File | null, answers: RightsAnswers,
  maxBytes: number): string | null;
export function withPhotoFields(fields: { kind: string; name: string; description: string },
  answers: RightsAnswers): [string, string][];
export function itemPhotos(item: PropItem): PropPhoto[];
export function castSlug(entry: CastEntry): string;
export function shotDraft(shot: ShotPlanShot): ShotDraft;
export function togglePick(list: string[], slug: string, max?: number): string[];
export function toggleCastEntry(entries: CastEntry[], slug: string, max?: number): CastEntry[];
export function draftValue<K extends keyof ShotDraft>(shot: ShotPlanShot, draft: Partial<ShotDraft> | undefined,
  field: K): ShotDraft[K];
export function shotChanges(shot: ShotPlanShot, draft: Partial<ShotDraft> | undefined): ShotChanges;
export function shotPlanChanges(shots: ShotPlanShot[], drafts: Record<string, Partial<ShotDraft>>,
  note?: string): { shots: Record<string, ShotChanges>; note?: string } | null;
export function draftProblem(shots: ShotPlanShot[], drafts: Record<string, Partial<ShotDraft>>): string | null;
export function historyText(entry: ShotPlanEdit): string;
export function historyValue(field: string, value: unknown, names?: Record<string, string>): string;
export function planStateText(view: ShotPlanView | null | undefined): string;
export function savedText(
  response: ShotPlanSaveResponse | null | undefined,
  options?: { detail?: boolean },
): { tone: 'ok' | 'problem'; text: string } | null;
