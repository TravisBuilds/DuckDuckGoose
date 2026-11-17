export type CastHue = 'blue' | 'mustard' | 'aqua' | 'coral' | 'green' | 'lilac';

export interface CastColour {
  /** 'ink' for the narrator. */
  colour: CastHue | 'ink';
  /** True from the seventh character on: the same hues again, with a striped dot. */
  patterned: boolean;
  /** Position in creation order; -1 for the narrator. */
  index: number;
  narrator: boolean;
}

export interface CastMember {
  slug?: string | null;
  name?: string | null;
  created_at?: string | null;
}

export declare const CAST_COLOURS: CastHue[];
export declare const NARRATOR: CastColour;
export declare function castColourAt(index: number): CastColour;
export declare function creationOrder<T extends CastMember>(characters: T[] | null | undefined): T[];
export declare function castColours(characters: CastMember[] | null | undefined): Record<string, CastColour>;
export declare function isNarrator(speaker: string | null | undefined): boolean;
export declare function speakerColours(
  speakers: (string | null | undefined)[] | null | undefined,
  characters?: CastMember[],
): Record<string, CastColour>;
