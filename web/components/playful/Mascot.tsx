import { DucklingArt, GooseArt, type ArtProps } from './art';

export type MascotKind = 'goose' | 'duckling';

/**
 * The studio's hosts (DESIGN-TARGETS-v2 §5): Studio Goose (logo, guide, empty states) and the
 * Duckling with its clapperboard ("make something" empty states). `nod` plays the goose's nod once.
 */
export default function Mascot({ kind = 'goose', nod = false, className = '', ...rest }: ArtProps & {
  kind?: MascotKind;
  nod?: boolean;
}) {
  const Art = kind === 'duckling' ? DucklingArt : GooseArt;
  return <Art className={`${nod ? 'pf-nod' : ''} ${className}`.trim()} {...rest} />;
}
