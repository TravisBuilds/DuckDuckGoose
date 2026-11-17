import type { ReactNode, SVGProps } from 'react';

/**
 * Original illustrations from the design sprite (DESIGN-TARGETS-v2 §5), as inline SVG: no image
 * weight, no requests. Decorative unless given a title. Beans and the cat take their body colour
 * from `currentColor`, so a cast colour class tints them.
 */
export type ArtProps = Omit<SVGProps<SVGSVGElement>, 'viewBox'> & { title?: string };

function Art({ title, viewBox, children, ...rest }: ArtProps & { viewBox: string; children: ReactNode }) {
  return (
    <svg viewBox={viewBox} xmlns="http://www.w3.org/2000/svg" focusable="false"
      role={title ? 'img' : undefined} aria-hidden={title ? undefined : true} {...rest}>
      {title && <title>{title}</title>}
      {children}
    </svg>
  );
}

const INK = '#1F2A44';

export function GooseArt(props: ArtProps) {
  return (
    <Art viewBox="0 0 220 240" {...props}>
      <ellipse cx="110" cy="226" rx="70" ry="9" fill="rgba(31,42,68,.15)" />
      <path d="M84 196 l-6 26 h-16 M124 196 l4 26 h16" stroke="#F29A3A" strokeWidth="7" strokeLinecap="round" strokeLinejoin="round" fill="none" />
      <path d="M40 150 C30 110 80 108 120 118 C150 124 190 128 186 160 C182 196 140 206 104 204 C66 202 46 184 40 150Z" fill="#fff" stroke={INK} strokeWidth="5" />
      <path d="M70 150 C86 132 126 136 140 156 C120 170 92 172 70 150Z" fill="#EEF2F7" stroke={INK} strokeWidth="4" />
      <g className="pf-goose-head">
        <path d="M132 124 C126 96 128 74 136 56" stroke={INK} strokeWidth="30" strokeLinecap="round" fill="none" />
        <path d="M132 124 C126 96 128 74 136 56" stroke="#fff" strokeWidth="21" strokeLinecap="round" fill="none" />
        <circle cx="142" cy="48" r="28" fill="#fff" stroke={INK} strokeWidth="5" />
        <path d="M166 44 C186 40 202 46 204 54 C196 62 180 62 166 58Z" fill="#F29A3A" stroke={INK} strokeWidth="4" strokeLinejoin="round" />
        <circle cx="152" cy="40" r="6" fill={INK} />
        <circle cx="154" cy="38" r="2" fill="#fff" />
        <circle cx="136" cy="56" r="6" fill="#F7B7A8" opacity=".8" />
        <path d="M112 30 C116 12 160 6 172 22 C166 30 130 32 112 30Z" fill="#4F86C6" stroke={INK} strokeWidth="4" strokeLinejoin="round" />
        <circle cx="146" cy="10" r="5" fill="#D9BC6E" stroke={INK} strokeWidth="3" />
      </g>
      <path d="M112 100 C124 112 146 112 156 98 L160 112 C146 124 120 122 108 112Z" fill="#E8534A" stroke={INK} strokeWidth="4" strokeLinejoin="round" />
      <path d="M150 112 l10 26 l12 -6 l-10 -22Z" fill="#E8534A" stroke={INK} strokeWidth="4" strokeLinejoin="round" />
    </Art>
  );
}

export function DucklingArt(props: ArtProps) {
  return (
    <Art viewBox="0 0 140 150" {...props}>
      <ellipse cx="70" cy="142" rx="44" ry="6" fill="rgba(31,42,68,.15)" />
      <path d="M56 124 v14 M84 124 v14" stroke="#F29A3A" strokeWidth="6" strokeLinecap="round" />
      <ellipse cx="70" cy="100" rx="48" ry="34" fill="#F7D460" stroke={INK} strokeWidth="5" />
      <circle cx="72" cy="52" r="32" fill="#F7D460" stroke={INK} strokeWidth="5" />
      <path d="M96 54 C114 50 124 56 122 62 C114 68 104 66 96 64Z" fill="#F29A3A" stroke={INK} strokeWidth="4" strokeLinejoin="round" />
      <circle cx="84" cy="46" r="5.5" fill={INK} />
      <circle cx="86" cy="44" r="1.8" fill="#fff" />
      <path d="M64 22 C60 10 72 8 74 20" stroke={INK} strokeWidth="4" fill="#F7D460" />
      <rect x="20" y="86" width="40" height="30" rx="5" fill={INK} />
      <rect x="20" y="78" width="40" height="10" rx="3" fill="#fff" stroke={INK} strokeWidth="3" />
      <path d="M28 78 l6 10 M40 78 l6 10 M52 78 l6 10" stroke={INK} strokeWidth="3" />
    </Art>
  );
}

/** A generic stand-in character; `variant` 2 wears a hat. Only until real photos or sheets exist. */
export function BeanArt({ variant = 1, ...props }: ArtProps & { variant?: 1 | 2 }) {
  if (variant === 2) {
    return (
      <Art viewBox="0 0 120 150" {...props}>
        <ellipse cx="60" cy="144" rx="36" ry="5" fill="rgba(31,42,68,.15)" />
        <path d="M18 140 C16 96 30 78 60 78 C90 78 104 96 102 140Z" fill="currentColor" stroke={INK} strokeWidth="4" />
        <circle cx="60" cy="50" r="30" fill="#C99472" stroke={INK} strokeWidth="4" />
        <path d="M28 40 h64 l-6 -18 h-52Z" fill={INK} />
        <rect x="20" y="38" width="80" height="7" rx="3" fill={INK} />
        <circle cx="50" cy="56" r="4" fill={INK} />
        <circle cx="70" cy="56" r="4" fill={INK} />
        <path d="M46 70 Q60 64 74 70" stroke={INK} strokeWidth="3.5" fill="none" strokeLinecap="round" />
        <path d="M40 74 Q60 92 80 74" fill="#E9E4DA" stroke={INK} strokeWidth="3" />
      </Art>
    );
  }
  return (
    <Art viewBox="0 0 120 150" {...props}>
      <ellipse cx="60" cy="144" rx="36" ry="5" fill="rgba(31,42,68,.15)" />
      <path d="M22 140 C18 100 26 76 60 76 C94 76 102 100 98 140Z" fill="currentColor" stroke={INK} strokeWidth="4" />
      <circle cx="60" cy="48" r="30" fill="#F4CFAE" stroke={INK} strokeWidth="4" />
      <path d="M30 46 C28 18 56 8 76 18 C92 26 92 40 90 46 C78 34 54 30 30 46Z" fill="#3B2A20" stroke={INK} strokeWidth="3" />
      <circle cx="50" cy="52" r="4" fill={INK} />
      <circle cx="70" cy="52" r="4" fill={INK} />
      <path d="M50 64 Q60 72 70 64" stroke={INK} strokeWidth="3.5" fill="none" strokeLinecap="round" />
      <circle cx="44" cy="62" r="5" fill="#F29A8A" opacity=".6" />
      <circle cx="76" cy="62" r="5" fill="#F29A8A" opacity=".6" />
    </Art>
  );
}

export function CatArt(props: ArtProps) {
  return (
    <Art viewBox="0 0 120 120" {...props}>
      <ellipse cx="60" cy="114" rx="36" ry="5" fill="rgba(31,42,68,.15)" />
      <path d="M24 110 C20 76 36 62 60 62 C84 62 100 76 96 110Z" fill="currentColor" stroke={INK} strokeWidth="4" />
      <path d="M30 40 L34 10 L52 28 M90 40 L86 10 L68 28" fill="currentColor" stroke={INK} strokeWidth="4" strokeLinejoin="round" />
      <circle cx="60" cy="46" r="28" fill="currentColor" stroke={INK} strokeWidth="4" />
      <circle cx="50" cy="44" r="4" fill={INK} />
      <circle cx="70" cy="44" r="4" fill={INK} />
      <path d="M56 54 h8 l-4 4Z" fill="#E8534A" />
      <path d="M40 56 h-14 M40 60 h-12 M80 56 h14 M80 60 h12" stroke={INK} strokeWidth="2" />
    </Art>
  );
}

/** A place stand-in. */
export function KilnArt(props: ArtProps) {
  return (
    <Art viewBox="0 0 120 100" {...props}>
      <path d="M14 92 V50 L60 16 L106 50 V92Z" fill="#E8534A" stroke={INK} strokeWidth="4" strokeLinejoin="round" />
      <rect x="76" y="10" width="14" height="26" fill="#C98942" stroke={INK} strokeWidth="4" />
      <path d="M44 92 V66 a16 16 0 0 1 32 0 V92Z" fill="#F7D460" stroke={INK} strokeWidth="4" />
      <circle cx="34" cy="62" r="6" fill="#FFF8EC" stroke={INK} strokeWidth="3" />
    </Art>
  );
}

/** A prop stand-in. */
export function BellArt(props: ArtProps) {
  return (
    <Art viewBox="0 0 100 100" {...props}>
      <path d="M50 12 C30 12 24 32 24 50 C24 64 16 70 14 76 H86 C84 70 76 64 76 50 C76 32 70 12 50 12Z" fill="#D9BC6E" stroke={INK} strokeWidth="4" strokeLinejoin="round" />
      <circle cx="50" cy="84" r="8" fill="#C98942" stroke={INK} strokeWidth="4" />
      <rect x="44" y="4" width="12" height="10" rx="3" fill={INK} />
    </Art>
  );
}

/** A second place stand-in. */
export function BoatArt(props: ArtProps) {
  return (
    <Art viewBox="0 0 120 100" {...props}>
      <path d="M10 64 H110 L94 88 H26Z" fill="#4F86C6" stroke={INK} strokeWidth="4" strokeLinejoin="round" />
      <path d="M60 64 V10 L96 56Z" fill="#fff" stroke={INK} strokeWidth="4" strokeLinejoin="round" />
      <path d="M56 22 L28 56 H56Z" fill="#77C6D3" stroke={INK} strokeWidth="4" strokeLinejoin="round" />
    </Art>
  );
}
