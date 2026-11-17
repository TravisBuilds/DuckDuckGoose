export declare const TOKENS: Record<string, string>;
export declare const REMAP: Record<'gray' | 'blue' | 'red' | 'green' | 'amber' | 'yellow', Record<number, string>>;
export declare const CAST_GRADIENTS: Record<import('./castColour.mjs').CastHue | 'ink', [string, string]>;
export declare function luminance(hex: string): number;
export declare function contrastRatio(a: string, b: string): number;
export declare const TEXT_PAIRS: { text: string; background: string; ratio: number }[];
