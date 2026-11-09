// Small helpers looks and scenes share.

import React from 'react';
import {Easing, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {useLook} from './context.tsx';
import type {ResolvedMotion} from './types.ts';

export const clamp01 = (x: number): number => Math.min(1, Math.max(0, x));
export const easeIO = Easing.bezier(0.65, 0, 0.35, 1);

export const fill: React.CSSProperties = {position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover'};

/** A slow camera move across `frames`: it holds its end state afterwards. */
export const Motion: React.FC<{motion: ResolvedMotion | null; frames: number; children: React.ReactNode}> = ({motion, frames, children}) => {
  const frame = useCurrentFrame();
  if (!motion) return <div style={{position: 'absolute', inset: 0}}>{children}</div>;
  const t = interpolate(frame, [0, Math.max(1, frames)], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const mix = ([a, b]: [number, number]) => a + (b - a) * t;
  const shift = motion.x[0] || motion.x[1] || motion.y[0] || motion.y[1] ? `translate(${mix(motion.x)}%, ${mix(motion.y)}%) ` : '';
  return (
    <div style={{position: 'absolute', inset: 0, transform: `${shift}scale(${mix(motion.scale)})`, transformOrigin: motion.origin}}>
      {children}
    </div>
  );
};

/** Push-in for scenes that manage their own framing. */
export const PushIn: React.FC<{frames: number; from?: number; to?: number; origin?: string; children: React.ReactNode}> = ({
  frames, from = 1, to = 1.06, origin = '50% 40%', children,
}) => <Motion motion={{scale: [from, to], x: [0, 0], y: [0, 0], origin}} frames={frames}>{children}</Motion>;

/** The job's look, reduced to what a ready-made scene needs to dress itself: colours, type and
 *  the feel of a surface. `u` scales sizes drawn for a 1080-wide frame to the canvas. */
export type Dress = {
  ink: string;
  paper: string;
  accent: string;
  negative: string;
  positive: string;
  font: string;
  body: string;
  weight: number;
  radius: number;
  border: number;
  shadow: 'hard' | 'soft' | 'none';
  offset: number;
  u: number;
};

export const useDress = (): Dress => {
  const {tokens} = useLook();
  const u = useVideoConfig().width / 1080;
  return {
    ink: tokens.color.ink,
    paper: tokens.color.paper,
    accent: tokens.color.accent,
    negative: tokens.color.negative ?? '#FF4D4D',
    positive: tokens.color.positive ?? '#2ECC71',
    font: tokens.font.display,
    body: tokens.font.body ?? tokens.font.display,
    weight: tokens.font.weight ?? 700,
    radius: (tokens.shape?.radius ?? 28) * u,
    border: (tokens.shape?.border ?? 0) * u,
    shadow: tokens.shape?.shadow ?? 'soft',
    offset: (tokens.shape?.offset ?? 12) * u,
    u,
  };
};

/** A card, chip or panel in the look's own manner: hard offset shadow and a border, or a soft drop. */
export const surface = (d: Dress, opts: {bg?: string; fg?: string; radius?: number} = {}): React.CSSProperties => ({
  background: opts.bg ?? d.paper,
  color: opts.fg ?? d.ink,
  borderRadius: opts.radius ?? d.radius,
  border: d.border ? `${d.border}px solid ${d.ink}` : undefined,
  boxShadow: d.shadow === 'hard' ? `${d.offset}px ${d.offset}px 0 ${d.ink}` : d.shadow === 'soft' ? `0 ${18 * d.u}px ${48 * d.u}px rgba(0,0,0,0.35)` : undefined,
  boxSizing: 'border-box',
});

/** Frames a scene's own entrance is already into when its first frame shows. A video's first
 *  frame has to work on its own, so nothing a scene shows may start from nothing. */
export const HEAD_START = 5;

/** The largest font size at which `text` fits `width` on one line, by an average glyph width. */
export const fitSize = (text: string, width: number, max: number, glyph = 0.6): number =>
  Math.min(max, width / (glyph * Math.max(1, text.length)));
