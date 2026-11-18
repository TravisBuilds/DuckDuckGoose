#!/usr/bin/env node
/**
 * Makes the home page's pictures (public/home/) from the direction deck's photos: every width in
 * lib/home-pictures.mjs as AVIF and WebP, without metadata.
 *
 *   unzip duckduckgoose-direction-team.pptx 'ppt/media/*' -d /tmp/deck
 *   node scripts/home-pictures.mjs /tmp/deck/ppt/media
 *
 * sharp comes with next (its image optimizer), so there is nothing extra to install.
 */
import { mkdir, stat } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import sharp from 'sharp';
import { HOME_PICTURES, pictureUrl } from '../lib/home-pictures.mjs';

const ENCODE = {
  avif: { quality: 52, effort: 7, chromaSubsampling: '4:2:0' },
  webp: { quality: 74, effort: 6, smartSubsample: true },
};

const sourceDir = process.argv[2];
if (!sourceDir) {
  console.error('usage: node scripts/home-pictures.mjs <folder with the deck photos (ppt/media)>');
  process.exit(1);
}

const publicDir = fileURLToPath(new URL('../public', import.meta.url));
await mkdir(join(publicDir, 'home'), { recursive: true });

for (const [name, picture] of Object.entries(HOME_PICTURES)) {
  const source = join(sourceDir, picture.source);
  const { width, height } = await sharp(source).metadata();
  if (width !== picture.width || height !== picture.height) {
    throw new Error(`${picture.source} is ${width}x${height}, lib/home-pictures.mjs says ${picture.width}x${picture.height}`);
  }
  for (const size of picture.widths) {
    for (const [format, options] of Object.entries(ENCODE)) {
      const out = join(publicDir, pictureUrl(name, size, format));
      await sharp(source).resize({ width: size, withoutEnlargement: true }).toFormat(format, options).toFile(out);
      console.log(`${out.slice(publicDir.length)}  ${((await stat(out)).size / 1024).toFixed(1)} KB`);
    }
  }
}
