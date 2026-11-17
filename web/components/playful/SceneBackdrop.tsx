import type { HTMLAttributes } from 'react';

/**
 * The sky-and-floor scene behind heroes and banners. Pure CSS (gradient sky, radial-gradient
 * clouds, striped wood floor), so it costs no image weight.
 */
export default function SceneBackdrop({ floor = true, floorHeight, className = '', children, ...rest }:
  HTMLAttributes<HTMLDivElement> & { floor?: boolean; floorHeight?: number | string }) {
  return (
    <div className={`pf-scene ${className}`} {...rest}>
      {floor && <div className="pf-floor" aria-hidden="true" style={floorHeight !== undefined ? { height: floorHeight } : undefined} />}
      {children}
    </div>
  );
}
