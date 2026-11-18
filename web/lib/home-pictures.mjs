/**
 * The home page's pictures: the generated photos from the direction deck (no real people, no brands),
 * served from public/home/ as AVIF and WebP at each width below. scripts/home-pictures.mjs makes the
 * files; their alt text is in lib/home-copy.mjs.
 */
export const HOME_PICTURES = {
  driver: { source: 'g1-road.jpeg', width: 864, height: 1152, widths: [320, 480, 640] },
  dancer: { source: 'g1-dance.jpeg', width: 864, height: 1152, widths: [320, 480, 640] },
  home: { source: 'g1-home.jpeg', width: 864, height: 1152, widths: [400, 640, 864] },
  stage: { source: 'g2-stage.jpeg', width: 1024, height: 1024, widths: [480, 720, 1024] },
  meadow: { source: 'g2-meadow.jpeg', width: 1024, height: 1024, widths: [480, 720, 1024] },
};

export const PICTURE_FORMATS = ['avif', 'webp'];

/** The public URL of one size of a picture, e.g. /home/driver-480.avif. */
export function pictureUrl(name, width, format) {
  return `/home/${name}-${width}.${format}`;
}
