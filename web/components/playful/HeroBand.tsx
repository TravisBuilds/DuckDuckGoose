import type { HTMLAttributes } from 'react';

/**
 * The light-blue header band behind heroes and banners, with two big soft circles (lime and cyan)
 * in its corners. Pure CSS (globals.css .pf-band), so it costs no image weight.
 */
export default function HeroBand({ className = '', children, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return <div className={`pf-band ${className}`} {...rest}>{children}</div>;
}
