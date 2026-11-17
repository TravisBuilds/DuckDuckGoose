import { useId } from 'react';

/** The studio's mark: an azure rounded square with a play triangle and a lime dot. Decorative; the link around it names it. */
export default function LogoMark({ className = '' }: { className?: string }) {
  const fill = useId();
  return (
    <svg viewBox="0 0 36 36" className={className} aria-hidden="true" focusable="false" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <linearGradient id={fill} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#1E9BF0" />
          <stop offset="1" stopColor="#0A6CC0" />
        </linearGradient>
      </defs>
      <rect x="1" y="3" width="30" height="30" rx="10" fill={`url(#${fill})`} />
      <path d="M13 11.5v13l10-6.5z" fill="#fff" stroke="#fff" strokeWidth="1.5" strokeLinejoin="round" />
      <circle cx="29" cy="7" r="5.5" fill="#C6F432" stroke="#fff" strokeWidth="2" />
    </svg>
  );
}
