import type { Tone } from './ui';

export interface Badge {
  key: 'locked' | 'drift' | 'older' | 'unlocked' | 'photo' | 'no-photo';
  label: string;
  tone: Tone;
}

/** A character version as the badges read it. */
export interface BadgeVersion {
  version: number;
  locked: boolean;
  problems?: string[] | null;
}

export interface BadgeCharacter {
  name: string;
  default_version?: number;
  versions?: BadgeVersion[] | null;
}

export interface UniverseCounts {
  characters?: number;
  locked_characters?: number;
  locked_versions?: number;
  places?: number;
  props?: number;
  episodes?: number;
  finished_episodes?: number;
}

export declare function characterBadge(version: BadgeVersion | null | undefined, latestLocked?: number | null): Badge;
export declare function characterSummaryBadge(character: { versions?: BadgeVersion[] | null } | null | undefined): Badge;
export declare function stripBadge(
  entry: (BadgeVersion & { latest_locked?: number | null }) | null | undefined,
): Badge;
export declare function itemBadge(
  item: { photos?: number | unknown[] | null; primary_photo?: unknown; image?: unknown } | null | undefined,
): Badge;
export declare function driftCount(characters: { versions?: BadgeVersion[] | null }[] | null | undefined): number;
export declare function healthLine(counts: UniverseCounts | null | undefined, drift?: number): string;
export declare function countsLine(counts: UniverseCounts | null | undefined): string;
export declare function usedInLabel(usedIn: number | { episodes?: number } | null | undefined): string;
export declare function sameName(name: string | null | undefined): string;
export declare function autoMapCast(
  rows: { code: string; name: string }[] | null | undefined,
  characters: { slug: string; name: string }[] | null | undefined,
): Record<string, string>;
export declare function castMatchText(
  character: BadgeCharacter | null | undefined,
): { matched: boolean; text: string; badge: Badge | null };
