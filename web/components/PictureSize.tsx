'use client';

import { useEffect, useState } from 'react';
import { pictureSizeCheck } from '@/lib/cast.mjs';
import type { PictureSizeLimits } from '@/lib/cast.mjs';

type Size = { width: number; height: number };

/**
 * A chosen picture's width and height, read in the browser: null until it is read, or when the
 * browser cannot read it (the API decodes every upload again and has the last word).
 */
export function usePictureSize(file: File | null): Size | null {
  const [read, setRead] = useState<{ file: File; size: Size } | null>(null);
  useEffect(() => {
    if (!file || typeof window === 'undefined' || typeof URL.createObjectURL !== 'function') return;
    let current = true;
    const url = URL.createObjectURL(file);
    const picture = new window.Image();
    picture.onload = () => {
      if (current) setRead({ file, size: { width: picture.naturalWidth, height: picture.naturalHeight } });
      URL.revokeObjectURL(url);
    };
    picture.onerror = () => URL.revokeObjectURL(url);
    picture.src = url;
    return () => {
      current = false;
    };
  }, [file]);
  return read && read.file === file ? read.size : null;
}

/** The size check of a chosen picture: a problem that stops the upload, or a plain note. */
export function usePictureCheck(file: File | null, limits: PictureSizeLimits | null | undefined) {
  const size = usePictureSize(file);
  return file && size ? pictureSizeCheck(size.width, size.height, limits) : { problem: null, note: null };
}

/** The plain note for a small picture, chosen or kept ("Small image, we'll sharpen it up."). */
export function SizeNote({ text, testId, className = '' }: { text: string | null | undefined; testId?: string; className?: string }) {
  if (!text) return null;
  return (
    <p data-testid={testId} data-size-note="" className={`text-[12px] font-bold text-blue-ink ${className}`}>
      {text}
    </p>
  );
}

/** For a chosen picture that is sent later (the API checks it then): its size note, or why it is too small. */
export function ChosenPictureSize({ file, limits, testId, className = '' }: {
  file: File | null; limits: PictureSizeLimits | null | undefined; testId?: string; className?: string;
}) {
  const check = usePictureCheck(file, limits);
  if (check.problem) {
    return <p data-testid={testId} className={`text-[12px] font-bold text-coral-ink ${className}`}>{check.problem}</p>;
  }
  return <SizeNote text={check.note} testId={testId} className={className} />;
}
