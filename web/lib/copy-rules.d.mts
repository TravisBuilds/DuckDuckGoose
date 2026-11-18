export declare const BANNED_WORDS: { word: string; pattern: RegExp }[];
export declare const OFF_LIMITS: { topic: string; pattern: RegExp }[];
export declare function copyProblems(text: string): { rule: string; match: string }[];
