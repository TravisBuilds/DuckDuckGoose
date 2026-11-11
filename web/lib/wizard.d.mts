export declare const FREEFORM_LENGTH_RANGE: [number, number];
export declare const FREEFORM_DEFAULT_LENGTH: number;
export declare const TEMPLATE_LABELS: Record<string, string>;

/** One entry of draft-from-idea's voice_slots (hfvg.genre_drafts.voice_slots). */
export interface VoiceSlot {
  key: string;
  role: 'narrator' | 'speaker';
  label: string;
  speaker: string | null;
  lines: number;
  suggestion: string;
}

/** POST /api/voices/pick response. */
export interface VoicePick {
  voice_id: string;
  name: string;
  preview_url?: string | null;
  labels?: Record<string, string>;
  description?: string;
  method?: string;
  reason?: string;
  remaining?: number;
  mode?: string;
}

export type VoiceChoices = Record<string, Pick<VoicePick, 'voice_id'> | null | undefined>;

export declare function lengthOptions(range: [number, number], defaultLength: number): number[];
export declare function formatAspect(aspect: string): string;
export declare function soundLabel(preset: { voiceOver: string; music: string }): string;
export declare function groupByTemplate<T extends { template: string }>(
  presets: T[]
): { template: string; label: string; presets: T[] }[];
export declare function briefFieldLabel(field: string): string;
export declare function composeIdea(idea: string, briefFields: string[], answers: Record<string, string>): string;
export declare function slotToken(key: string): string;
export declare function missingVoiceSlots(slots: VoiceSlot[], choices: VoiceChoices): VoiceSlot[];
export declare function voiceBlockMessage(
  slots: VoiceSlot[],
  choices: VoiceChoices,
  defaultVoiceAvailable: boolean
): string | null;
export declare function applyVoiceChoices<P>(plan: P, slots: VoiceSlot[], choices: VoiceChoices): P;

/** The create wizard's "Text on screen" switches. */
export interface OnScreenTextChoices {
  subtitles: boolean;
  titleCard: boolean;
  titleText: string;
  endCard: boolean;
  endText: string;
}

export declare const TEXT_CARD_MAX_CHARS: number;
export declare const END_CARD_TEXT: string;
export declare function cardTiming(totalSeconds: number): { visible_s?: number; fade_s?: number };
export declare function hasVoiceLines(plan: unknown): boolean;
export declare function onScreenTextDefaults(
  suggested: readonly string[] | null | undefined,
  plan: unknown,
  title: string | null | undefined
): OnScreenTextChoices;
export declare function onScreenTextProblem(choices: OnScreenTextChoices | null | undefined): string | null;
export declare function applyOnScreenText<P>(plan: P, choices: OnScreenTextChoices, totalSeconds: number): P;
