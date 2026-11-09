// The "pop" look: chunky creator style. Hard offset shadows, one loud accent, heavy type,
// springy pops. Restyle it here first: colours, type and shape carry most of a brand.
// This file is plain data (no imports), so the kit command can read it too.

export const tokens = {
  color: {
    accent: '#FFD400',
    ink: '#121212',
    paper: '#FFFFFF',
    negative: '#FF5A5F',
    positive: '#2ECC71',
  },
  font: {
    // DejaVu Sans Bold is installed on the render box; no font file to load.
    display: '"DejaVu Sans", "Helvetica Neue", "Arial Black", Arial, sans-serif',
    weight: 800,
  },
  shape: {border: 8, radius: 44, offset: 14, shadow: 'hard' as const},
  caption: {maxSize: 86},
};

export type Tokens = typeof tokens;

/** Facts the kit command checks without rendering. */
export const meta = {
  cardZones: {hook: 'upper', cta: 'upper', end: 'full'},
  captions: {maxWords: 4, maxChars: 24},
  contrastPairs: [
    {name: 'caption text on its outline', fg: tokens.color.paper, bg: tokens.color.ink},
    {name: 'spoken caption word on its outline', fg: tokens.color.accent, bg: tokens.color.ink},
    {name: 'tag text on the tag', fg: tokens.color.ink, bg: tokens.color.accent},
    {name: 'CTA title on the CTA card', fg: tokens.color.ink, bg: tokens.color.accent},
    {name: 'CTA accent word on the CTA card', fg: tokens.color.negative, bg: tokens.color.accent},
    {name: 'CTA button text on the button', fg: tokens.color.paper, bg: tokens.color.ink},
    {name: 'end card title on the end card', fg: tokens.color.ink, bg: tokens.color.paper},
    {name: 'end card badge text on the badge', fg: tokens.color.paper, bg: tokens.color.positive},
  ],
};
