// Safe areas and the named zones overlays and captions sit in. Everything scales with the
// canvas, so a square or landscape video gets the same proportions as 1080x1920.

import type {Rect, ZoneName} from './types.ts';

type Margins = {top: number; bottom: number; left: number; right: number};

// Fractions of the canvas an app's own interface covers.
const PROFILES: Record<string, Margins> = {
  // TikTok: caption and buttons at the bottom, the icon column on the right.
  tiktok: {top: 120 / 1920, bottom: 330 / 1920, left: 60 / 1080, right: 120 / 1080},
  reels: {top: 220 / 1920, bottom: 340 / 1920, left: 60 / 1080, right: 120 / 1080},
  shorts: {top: 130 / 1920, bottom: 300 / 1920, left: 60 / 1080, right: 130 / 1080},
  none: {top: 0.03, bottom: 0.03, left: 0.03, right: 0.03},
};

export const SAFE_PROFILES = Object.keys(PROFILES);

export function parseSize(size: string | undefined): {width: number; height: number} | null {
  const m = /^(\d{2,5})x(\d{2,5})$/.exec((size ?? '1080x1920').trim());
  return m ? {width: Number(m[1]), height: Number(m[2])} : null;
}

export function safeArea(width: number, height: number, profile: string | undefined): Margins {
  const p = PROFILES[profile ?? 'tiktok'] ?? PROFILES.tiktok;
  return {
    top: Math.round(p.top * height),
    bottom: Math.round(p.bottom * height),
    left: Math.round(p.left * width),
    right: Math.round(p.right * width),
  };
}

/** upper: tags and cards. center: big statements. lower: captions, clear of the app's buttons. */
export function zonesFor(width: number, height: number, profile: string | undefined): Record<ZoneName, Rect> {
  const safe = safeArea(width, height, profile);
  const side = Math.round(width * (90 / 1080));
  const upperH = Math.round(height * (580 / 1920));
  const centerH = Math.round(height * (460 / 1920));
  const lowerH = Math.round(height * (400 / 1920));
  const lowerBottom = height - safe.bottom - Math.round(height * (30 / 1920));
  const lowerRight = safe.right + Math.round(width * (60 / 1080));
  return {
    upper: {x: side, y: safe.top, w: width - 2 * side, h: upperH},
    center: {x: side, y: safe.top + upperH, w: width - 2 * side, h: centerH},
    lower: {x: side, y: lowerBottom - lowerH, w: width - side - lowerRight, h: lowerH},
    full: {x: 0, y: 0, w: width, h: height},
  };
}

export const overlaps = (a: Rect, b: Rect): boolean =>
  a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
