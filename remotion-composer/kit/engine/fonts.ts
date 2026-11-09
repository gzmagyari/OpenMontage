// Fonts ship with the kit and are registered from local files, so a render needs no network.
// Registration is deferred to a microtask: a delayRender handle created while the bundle is
// still being evaluated can be wiped by a second copy of Remotion's delay-render module, and
// the render then fails "not cleared" (measured upstream, see src/lib/localFonts.ts).

import {cancelRender, continueRender, delayRender} from 'remotion';
import caveatUrl from '@fontsource-variable/caveat/files/caveat-latin-wght-normal.woff2';
import interUrl from '@fontsource-variable/inter/files/inter-latin-wght-normal.woff2';
import montserratUrl from '@fontsource-variable/montserrat/files/montserrat-latin-wght-normal.woff2';
import antonUrl from '@fontsource/anton/files/anton-latin-400-normal.woff2';
import archivoBlackUrl from '@fontsource/archivo-black/files/archivo-black-latin-400-normal.woff2';
import bebasNeueUrl from '@fontsource/bebas-neue/files/bebas-neue-latin-400-normal.woff2';
import dmSerifDisplayUrl from '@fontsource/dm-serif-display/files/dm-serif-display-latin-400-normal.woff2';
import poppins500Url from '@fontsource/poppins/files/poppins-latin-500-normal.woff2';
import poppins700Url from '@fontsource/poppins/files/poppins-latin-700-normal.woff2';
import poppins800Url from '@fontsource/poppins/files/poppins-latin-800-normal.woff2';
import poppins900Url from '@fontsource/poppins/files/poppins-latin-900-normal.woff2';
import playfairItalicUrl from '../../src/fonts/PlayfairDisplay-Italic-latin.woff2';
import playfairUrl from '../../src/fonts/PlayfairDisplay-latin.woff2';
import spaceGroteskUrl from '../../src/fonts/SpaceGrotesk-latin.woff2';

const LATIN =
  'U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD';

const loaded = new Set<string>();

/** Registers one font file for the given weights and returns the family name to use in CSS.
 *  A weight is a number ("700") or, for a variable file, a range ("300 700"). */
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

const POPPINS: Record<string, string> = {'500': poppins500Url, '700': poppins700Url, '800': poppins800Url, '900': poppins900Url};

/** Families that ship with the kit (Latin; all under the SIL Open Font License). Call the one a
 *  look uses once, at the top of the look, and use the returned family name. System families
 *  (DejaVu, Noto, including the Noto families for other scripts) need no loading. */
export const fonts = {
  // Heavy display
  anton: () => registerFont('Anton', antonUrl),
  archivoBlack: () => registerFont('Archivo Black', archivoBlackUrl),
  bebasNeue: () => registerFont('Bebas Neue', bebasNeueUrl),
  // Sans, in any weight
  inter: () => registerFont('Inter', interUrl, {weights: ['100 900']}),
  montserrat: () => registerFont('Montserrat', montserratUrl, {weights: ['100 900']}),
  poppins: (weights: string[] = ['500', '700', '800']) => {
    for (const weight of weights) if (POPPINS[weight]) registerFont('Poppins', POPPINS[weight], {weights: [weight]});
    return 'Poppins';
  },
  spaceGrotesk: (weights: string[] = ['300 700']) => registerFont('Space Grotesk', spaceGroteskUrl, {weights}),
  // Serif
  dmSerifDisplay: () => registerFont('DM Serif Display', dmSerifDisplayUrl),
  playfair: (weights: string[] = ['400', '700']) => registerFont('Playfair Display', playfairUrl, {weights}),
  playfairItalic: (weights: string[] = ['400', '700']) => registerFont('Playfair Display', playfairItalicUrl, {weights, style: 'italic'}),
  // Handwritten
  caveat: () => registerFont('Caveat', caveatUrl, {weights: ['400 700']}),
};
