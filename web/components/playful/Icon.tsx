import type { ReactNode, SVGProps } from 'react';

export type IconName =
  | 'lock' | 'unlock' | 'play' | 'plus' | 'check' | 'spark' | 'photo' | 'mic' | 'film' | 'user'
  | 'arrow' | 'back' | 'close' | 'alert' | 'book' | 'note' | 'pin' | 'coins' | 'menu' | 'chevron';

/** Small UI icons (24px grid, currentColor). Decorative unless given a title. */
export default function Icon({ name, size = 16, title, ...rest }: Omit<SVGProps<SVGSVGElement>, 'name'> & {
  name: IconName; size?: number; title?: string;
}) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} xmlns="http://www.w3.org/2000/svg" focusable="false"
      role={title ? 'img' : undefined} aria-hidden={title ? undefined : true} {...rest}>
      {title && <title>{title}</title>}
      {PATHS[name]}
    </svg>
  );
}

const stroke = { stroke: 'currentColor', fill: 'none', strokeLinecap: 'round', strokeLinejoin: 'round' } as const;

const PATHS: Record<IconName, ReactNode> = {
  lock: (
    <>
      <rect x="4" y="10" width="16" height="11" rx="3" fill="currentColor" />
      <path d="M8 10V7a4 4 0 0 1 8 0v3" {...stroke} strokeWidth="2.6" />
    </>
  ),
  unlock: (
    <>
      <rect x="4" y="10" width="16" height="11" rx="3" fill="currentColor" />
      <path d="M8 10V7a4 4 0 0 1 7.5-2" {...stroke} strokeWidth="2.6" />
    </>
  ),
  play: (
    <>
      <circle cx="12" cy="12" r="11" fill="#fff" />
      <path d="M10 7.5v9l7-4.5z" fill="#1F2A44" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" {...stroke} strokeWidth="3" />,
  check: <path d="M5 12.5l4.5 4.5L19 7.5" {...stroke} strokeWidth="3.2" />,
  spark: <path d="M12 2l2.4 7.6L22 12l-7.6 2.4L12 22l-2.4-7.6L2 12l7.6-2.4z" fill="currentColor" />,
  photo: (
    <>
      <rect x="3" y="5" width="18" height="14" rx="3" {...stroke} strokeWidth="2.4" />
      <circle cx="9" cy="10" r="2" fill="currentColor" />
      <path d="M4 18l6-6 4 4 3-3 4 4" {...stroke} strokeWidth="2.2" />
    </>
  ),
  mic: (
    <>
      <rect x="9" y="3" width="6" height="11" rx="3" fill="currentColor" />
      <path d="M6 11a6 6 0 0 0 12 0M12 17v4" {...stroke} strokeWidth="2.2" />
    </>
  ),
  film: (
    <>
      <rect x="3" y="5" width="18" height="14" rx="3" {...stroke} strokeWidth="2.4" />
      <path d="M10 9.5v5l4.5-2.5z" fill="currentColor" />
    </>
  ),
  user: (
    <>
      <circle cx="12" cy="8.5" r="4" {...stroke} strokeWidth="2.4" />
      <path d="M4.5 20c1.2-3.6 4-5.5 7.5-5.5s6.3 1.9 7.5 5.5" {...stroke} strokeWidth="2.4" />
    </>
  ),
  arrow: <path d="M5 12h13M13 6l6 6-6 6" {...stroke} strokeWidth="2.6" />,
  back: <path d="M19 12H6M11 6l-6 6 6 6" {...stroke} strokeWidth="2.6" />,
  close: <path d="M6 6l12 12M18 6L6 18" {...stroke} strokeWidth="2.8" />,
  alert: (
    <>
      <path d="M12 3.5l9.5 16.5h-19z" {...stroke} strokeWidth="2.2" />
      <path d="M12 10v4.5" {...stroke} strokeWidth="2.6" />
      <circle cx="12" cy="17.3" r="1.4" fill="currentColor" />
    </>
  ),
  book: (
    <>
      <path d="M4 5.5C6.5 4 9.5 4 12 6c2.5-2 5.5-2 8-.5V19c-2.5-1.5-5.5-1.5-8 .5-2.5-2-5.5-2-8-.5z" {...stroke} strokeWidth="2.2" />
      <path d="M12 6v13.5" {...stroke} strokeWidth="2.2" />
    </>
  ),
  note: (
    <>
      <path d="M5 4h14v11l-5 5H5z" {...stroke} strokeWidth="2.2" />
      <path d="M14 20v-5h5M8.5 9h7M8.5 12.5h4" {...stroke} strokeWidth="2.2" />
    </>
  ),
  pin: (
    <>
      <circle cx="12" cy="9" r="5" fill="currentColor" />
      <path d="M12 14v7" {...stroke} strokeWidth="2.4" />
    </>
  ),
  coins: (
    <>
      <ellipse cx="12" cy="7" rx="7" ry="3" {...stroke} strokeWidth="2.2" />
      <path d="M5 7v5c0 1.7 3.1 3 7 3s7-1.3 7-3V7M5 12v5c0 1.7 3.1 3 7 3s7-1.3 7-3v-5" {...stroke} strokeWidth="2.2" />
    </>
  ),
  menu: <path d="M4 7h16M4 12h16M4 17h16" {...stroke} strokeWidth="2.6" />,
  chevron: <path d="M9 6l6 6-6 6" {...stroke} strokeWidth="2.6" />,
};
