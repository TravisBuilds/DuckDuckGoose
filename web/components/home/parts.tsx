import Link from 'next/link';
import type { CSSProperties } from 'react';
import { HOME_COPY } from '@/lib/home-copy.mjs';
import { HOME_PICTURES, pictureUrl, type HomePictureName, type PictureFormat } from '@/lib/home-pictures.mjs';
import { EARLY_ACCESS_HREF } from './links';
import styles from './home.module.css';

/** The direction deck's organic shapes, as paths in a 1 by 1 box. */
const PATHS = {
  /** Slide 1's lime and cyan blobs. */
  pebble:
    'M0.782,0.053 C0.846,0.095 0.896,0.158 0.911,0.228 C0.927,0.298 0.908,0.373 0.925,0.46 C0.941,0.546 0.993,0.642 0.999,0.751 C1.006,0.861 0.966,0.982 0.886,0.998 C0.806,1.014 0.684,0.926 0.596,0.865 C0.508,0.806 0.454,0.775 0.367,0.762 C0.28,0.75 0.162,0.755 0.103,0.706 C0.045,0.657 0.047,0.553 0.029,0.439 C0.011,0.325 -0.026,0.201 0.029,0.146 C0.085,0.091 0.233,0.108 0.338,0.09 C0.442,0.073 0.502,0.021 0.572,0.005 C0.641,-0.01 0.718,0.011 0.782,0.053 Z',
  /** Slide 2's tall photo frame. */
  tall: 'M0.938,0.408 C1.119,0.563 0.884,0.885 0.491,0.984 C0.098,1.082 0.23,0.701 0.068,0.465 C-0.099,0.224 0.039,0 0.491,0 C0.942,0 0.757,0.254 0.938,0.408 Z',
  /** Slides 18 and 41's round photo frame. */
  round:
    'M0.731,0.032 C0.792,0.072 0.827,0.161 0.875,0.244 C0.924,0.328 0.986,0.406 0.998,0.492 C1.01,0.578 0.973,0.671 0.917,0.74 C0.861,0.81 0.788,0.855 0.714,0.894 C0.641,0.934 0.569,0.967 0.486,0.986 C0.404,1.005 0.312,1.01 0.247,0.965 C0.181,0.921 0.143,0.827 0.097,0.739 C0.05,0.651 -0.006,0.567 0.001,0.488 C0.007,0.409 0.076,0.334 0.129,0.259 C0.183,0.183 0.222,0.108 0.281,0.066 C0.339,0.024 0.418,0.015 0.501,0.007 C0.583,-0.002 0.67,-0.009 0.731,0.032 Z',
  /** Slide 2's sky panel: straight on the right, its left edge bulging out. */
  bulge:
    'M0.156,0 L1,0 L1,1 L0.156,1 L0.152,0.995 C0.055,0.845 0,0.677 0,0.5 C0,0.323 0.055,0.155 0.152,0.005 Z',
  /** Slide 41's azure panel with a wavy left edge. */
  wave: 'M1,0 L1,1 L0.308,1 C0.292,0.864 -0.129,0.755 0.103,0.595 C0.26,0.487 0.219,0.405 0.062,0.245 C-0.013,0.169 -0.009,0.076 0.016,0 Z',
} as const;

type Shape = keyof typeof PATHS;
type FrameShape = 'tall' | 'round';

const clipId = (shape: FrameShape) => `home-clip-${shape}`;

/** Which screens a decoration is drawn on: narrow is below 960px, wide from 960px. */
const SHOW = { all: '', wide: styles.wideOnly, narrow: styles.narrowOnly };

/** A decorative shape stretched over its box; colour it with `fill` in CSS. */
export function Blob({ shape, className, show = 'all' }: { shape: Shape; className: string; show?: keyof typeof SHOW }) {
  return (
    <svg
      className={`${className} ${SHOW[show]}`}
      viewBox="0 0 1 1"
      preserveAspectRatio="none"
      aria-hidden="true"
      focusable="false"
    >
      <path d={PATHS[shape]} />
    </svg>
  );
}

/** The clip paths the framed pictures use. Rendered once per page. */
export function ClipPaths() {
  return (
    <svg className={styles.clipPaths} aria-hidden="true" focusable="false">
      <defs>
        {(['tall', 'round'] as const).map((shape) => (
          <clipPath key={shape} id={clipId(shape)} clipPathUnits="objectBoundingBox">
            <path d={PATHS[shape]} />
          </clipPath>
        ))}
      </defs>
    </svg>
  );
}

/**
 * A small coloured dot. `place` is its spot inside the section (top/left/right/bottom); dots shown
 * on phones sit in the section's padding, clear of the text.
 */
export function Dot({ color, size, place, show = 'all' }: {
  color: 'azure' | 'lime' | 'yellow' | 'red' | 'cyan';
  size: number;
  place: Pick<CSSProperties, 'top' | 'left' | 'right' | 'bottom'>;
  show?: keyof typeof SHOW;
}) {
  return (
    <span
      aria-hidden="true"
      className={`${styles.dot} ${SHOW[show]}`}
      style={{ ...place, width: size, height: size, background: `var(--home-${color})` }}
    />
  );
}

/** One of the deck's photos as AVIF with a WebP fallback, at the widths in lib/home-pictures.mjs. */
export function Picture({ name, alt, sizes, eager = false, className }: {
  name: HomePictureName;
  alt: string;
  sizes: string;
  eager?: boolean;
  className?: string;
}) {
  const { width, height, widths } = HOME_PICTURES[name];
  const srcSet = (format: PictureFormat) => widths.map((w) => `${pictureUrl(name, w, format)} ${w}w`).join(', ');
  const largest = widths[widths.length - 1];
  return (
    <picture>
      <source type="image/avif" srcSet={srcSet('avif')} sizes={sizes} />
      <img
        src={pictureUrl(name, largest, 'webp')}
        srcSet={srcSet('webp')}
        sizes={sizes}
        alt={alt}
        width={largest}
        height={Math.round((largest * height) / width)}
        loading={eager ? 'eager' : 'lazy'}
        decoding="async"
        className={className}
      />
    </picture>
  );
}

/** A photo cut to one of the deck's organic shapes, on a white shape a little larger. */
export function FramedPicture({ shape, name, alt, sizes, className }: {
  shape: FrameShape;
  name: HomePictureName;
  alt: string;
  sizes: string;
  className: string;
}) {
  const clip = `url(#${clipId(shape)})`;
  return (
    <div className={`${styles.frame} ${className}`}>
      <Blob shape={shape} className={styles.frameBack} />
      <div className={styles.framePhoto} style={{ clipPath: clip, WebkitClipPath: clip }}>
        <Picture name={name} alt={alt} sizes={sizes} className={styles.framePhotoImg} />
      </div>
    </div>
  );
}

/** "DuckDuckGoose", with "Goose" in azure. */
export function Wordmark() {
  return (
    <>
      {HOME_COPY.brand.first}
      <span className={styles.wordmarkTail}>{HOME_COPY.brand.second}</span>
    </>
  );
}

/** The page's one call to action. */
export function EarlyAccessLink({ label, size = 'large', className = '' }: {
  label: string;
  size?: 'large' | 'small';
  className?: string;
}) {
  return (
    <Link
      href={EARLY_ACCESS_HREF}
      prefetch={false}
      className={`pf-btn pf-btn-primary ${size === 'large' ? styles.ctaLarge : styles.ctaSmall} ${className}`}
    >
      {label}
    </Link>
  );
}
