export type Accent = 'lime' | 'yellow' | 'orange' | 'cyan' | 'sky';
export type IconLayer = 'fill' | 'pop' | 'paper' | 'line' | 'bold' | 'thin' | 'ink' | 'glint' | Accent;
export type IconName =
  | 'home' | 'universe' | 'episode' | 'cast' | 'place' | 'prop' | 'references' | 'moodboard' | 'bible'
  | 'lock' | 'voice' | 'music' | 'coins' | 'download' | 'search' | 'menu' | 'plus' | 'check' | 'arrow'
  | 'alert' | 'note' | 'spark' | 'photo' | 'film' | 'triangle' | 'exit';
export type IconShape = [IconLayer, 'path' | 'circle' | 'rect' | 'ellipse', Record<string, string | number>];
export interface IconDef {
  accent: Accent;
  pop?: Accent;
  shapes: IconShape[];
}

export declare const ACCENTS: Record<Accent, { fill: string; soft: string }>;
export declare const STROKE: number;
export declare function strokeFor(size: number): number;
export declare const ICONS: Record<IconName, IconDef>;
export declare const STEP_ICON: Record<string, IconName>;
export declare const ICON_GROUPS: Record<'tabBar' | 'heroTrio' | 'creationPath', IconName[]>;
export declare const OUTLINE_PAIRS: { outline: string; surface: string; where: string }[];
export declare function layerProps(
  layer: IconLayer,
  icon: IconDef,
  options?: { soft?: boolean; stroke?: number },
): {
  fill?: string;
  stroke?: string;
  strokeWidth?: number;
  strokeLinecap?: 'round';
  strokeLinejoin?: 'round';
  className?: string;
};
