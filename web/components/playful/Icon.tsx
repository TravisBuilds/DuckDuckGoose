import { createElement, type SVGProps } from 'react';
import { ICONS, layerProps, strokeFor, type IconName } from '@/lib/icons.mjs';

export type { IconName };

/**
 * One of the studio's chunky duotone icons (lib/icons.mjs): a currentColor outline over a fill in the
 * concept's own accent. `soft` is the lighter, inactive look (a tab you are not on, a step not reached
 * yet). Decorative unless given a title, which makes it an image with that name.
 */
export default function Icon({ name, size = 16, title, soft = false, ...rest }: Omit<SVGProps<SVGSVGElement>, 'name'> & {
  name: IconName;
  size?: number;
  title?: string;
  soft?: boolean;
}) {
  const icon = ICONS[name];
  const stroke = strokeFor(size);
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} xmlns="http://www.w3.org/2000/svg" focusable="false"
      role={title ? 'img' : undefined} aria-hidden={title ? undefined : true}
      data-icon={name} data-soft={soft ? 'true' : undefined} {...rest}>
      {title && <title>{title}</title>}
      {icon.shapes.map(([layer, tag, attrs], i) => createElement(tag, { key: i, ...layerProps(layer, icon, { soft, stroke }), ...attrs }))}
    </svg>
  );
}
