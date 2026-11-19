import type { ChecklistItem, ChecklistKind, ChecklistProgress, WorldChecklist, WorldPhoto, WorldStory } from './world.mjs';

export declare const KIND_LABELS: Record<ChecklistKind, string>;
export declare const KIND_CHIPS: Record<ChecklistKind, string>;
export declare const STATUS_TONES: Record<'ready' | 'needs' | 'optional', { edge: string; text: string }>;
export declare const ADD_KINDS: { kind: ChecklistKind; label: string }[];
export declare function progressLine(progress: ChecklistProgress | null | undefined): string;
export declare function progressPercent(progress: ChecklistProgress | null | undefined): number;
export declare function listReady(progress: ChecklistProgress | null | undefined): boolean;
export declare function countsLine(item: ChecklistItem): string;
export declare function itemStatus(item: ChecklistItem): { tone: 'ready' | 'optional' | 'needs'; text: string };
export declare function makeListLabel(story: WorldStory | null | undefined): string;
export declare function makeListBlocker(input: { saved: string; typed: string; storyMin?: number }): string | null;
export declare function itemPhotos(item: ChecklistItem | null | undefined, photos: WorldPhoto[] | null | undefined): WorldPhoto[];
export declare function otherPhotos(checklist: WorldChecklist | null | undefined, photos: WorldPhoto[] | null | undefined): WorldPhoto[];
export declare function listMadeMessage(result: { added?: string[]; removed?: string[]; dropped?: string[] } | null | undefined): string;
