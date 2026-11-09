// The "editorial" look: proof under a loupe. An ink-black frame, paper-white chips, and ONE
// signal colour for the spoken caption word and the call to action. Calm cuts, no bounce.
// Restyle it here first. This file is plain data (no imports), so the kit command can read it too.

export const tokens = {
  color: {
    ink: '#0D0D0F',
    paper: '#F5F2EB',
    // The one signal colour.
    accent: '#FFC23D',
    negative: '#FF4D3D',
    positive: '#8FD694',
  },
  font: {
    // Space Grotesk ships with the kit.
    display: 'Space Grotesk',
    weight: 700,
  },
  shape: {radius: 28, border: 0, offset: 0, shadow: 'soft' as const},
  caption: {size: 78},
};

export type Tokens = typeof tokens;

/** Facts the kit command checks without rendering. */
export const meta = {
  cardZones: {end: 'full'},
  captions: {maxWords: 4, maxChars: 22},
  contrastPairs: [
    {name: 'caption text on its plate', fg: tokens.color.paper, bg: tokens.color.ink},
    {name: 'spoken caption word on its plate', fg: tokens.color.accent, bg: tokens.color.ink},
    {name: 'chip text on the chip', fg: tokens.color.ink, bg: tokens.color.paper},
    {name: 'end card text on the frame', fg: tokens.color.paper, bg: tokens.color.ink},
    {name: 'button text on the button', fg: tokens.color.ink, bg: tokens.color.accent},
  ],
};
