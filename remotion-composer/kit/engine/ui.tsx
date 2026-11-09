// Small helpers looks and scenes share.

import React from 'react';
import {Easing, interpolate, useCurrentFrame} from 'remotion';
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
