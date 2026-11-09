// Fonts ship with the kit and are registered from local files, so a render needs no network.
// Registration is deferred to a microtask: a delayRender handle created while the bundle is
// still being evaluated can be wiped by a second copy of Remotion's delay-render module, and
// the render then fails "not cleared" (measured upstream, see src/lib/localFonts.ts).

import {cancelRender, continueRender, delayRender} from 'remotion';
import playfairItalicUrl from '../../src/fonts/PlayfairDisplay-Italic-latin.woff2';
import playfairUrl from '../../src/fonts/PlayfairDisplay-latin.woff2';
import spaceGroteskUrl from '../../src/fonts/SpaceGrotesk-latin.woff2';

const LATIN =
  'U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD';

const loaded = new Set<string>();

/** Registers one font file for the given weights and returns the family name to use in CSS. */
export function registerFont(
  family: string,
  url: string,
  opts: {weights?: string[]; style?: 'normal' | 'italic'; unicodeRange?: string} = {},
): string {
  if (typeof FontFace === 'undefined') return family;
  const style = opts.style ?? 'normal';
  for (const weight of opts.weights ?? ['400']) {
    const key = `${family}-${style}-${weight}`;
    if (loaded.has(key)) continue;
    loaded.add(key);
    queueMicrotask(() => {
      const handle = delayRender(`Loading font ${key}`);
      const face = new FontFace(family, `url(${url}) format('woff2')`, {weight, style, unicodeRange: opts.unicodeRange ?? LATIN});
      face
        .load()
        .then(() => {
          document.fonts.add(face);
          continueRender(handle);
        })
        // Never fall back to a system font silently: fail the render.
        .catch((err) => cancelRender(new Error(`Font ${key} failed to load: ${err}`)));
    });
  }
  return family;
}

/** Families that ship with the kit. System families (DejaVu, Noto) need no loading. */
export const fonts = {
  spaceGrotesk: (weights: string[] = ['400', '500', '700']) => registerFont('Space Grotesk', spaceGroteskUrl, {weights}),
  playfair: (weights: string[] = ['400', '700']) => registerFont('Playfair Display', playfairUrl, {weights}),
  playfairItalic: (weights: string[] = ['400', '700']) => registerFont('Playfair Display', playfairItalicUrl, {weights, style: 'italic'}),
};
