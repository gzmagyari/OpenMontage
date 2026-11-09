// One number that counts up to its value, with a line saying what it measures.

import React from 'react';
import {Easing, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {useShot} from '../engine/context.tsx';
import type {Rect} from '../engine/types.ts';
import {HEAD_START, fitSize, useDress} from '../engine/ui.tsx';
import {Backdrop, type BackdropSpec} from './Backdrop.tsx';

export type StatCountProps = {
  value: number;
  from?: number;
  prefix?: string;
  suffix?: string;
  decimals?: number;
  label?: string;
  /** How long the count takes (default: 1.2 s, at most 60% of the shot). */
  seconds?: number;
  backdrop?: BackdropSpec;
  /** Set by the engine when the number is used as an overlay. */
  zone?: Rect;
  frames?: number;
};

export const StatCount: React.FC<StatCountProps> = (p) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const shot = useShot();
  const d = useDress();
  const overlay = p.zone !== undefined;
  const total = p.frames ?? shot.frames;
  const box = p.zone ?? {x: 90 * d.u, y: height * 0.25, w: width - 180 * d.u, h: height * 0.4};
  const countF = Math.max(6, Math.min(Math.round((p.seconds ?? 1.2) * fps), Math.round(total * 0.6)));
  const value = interpolate(frame, [3, 3 + countF], [p.from ?? 0, p.value], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.out(Easing.cubic)});
  const text = `${p.prefix ?? ''}${value.toLocaleString('en-US', {minimumFractionDigits: p.decimals ?? 0, maximumFractionDigits: p.decimals ?? 0})}${p.suffix ?? ''}`;
  const final = `${p.prefix ?? ''}${p.value.toLocaleString('en-US', {minimumFractionDigits: p.decimals ?? 0, maximumFractionDigits: p.decimals ?? 0})}${p.suffix ?? ''}`;
  const pop = spring({frame: frame + HEAD_START, fps, config: {damping: 14, stiffness: 170, mass: 0.6}});
  const shade = overlay ? '0 6px 24px rgba(0,0,0,0.55)' : undefined;
  return (
    <div style={{position: 'absolute', inset: 0}}>
      {overlay ? null : <Backdrop spec={p.backdrop} />}
      <div style={{position: 'absolute', left: box.x, top: box.y, width: box.w, height: box.h, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', opacity: Math.min(1, pop * 1.5), transform: `scale(${0.9 + 0.1 * pop})`}}>
        {/* Sized for the final value, so the number does not jump while it counts. */}
        <div style={{fontFamily: d.font, fontWeight: d.weight, fontSize: fitSize(final, box.w, 300 * d.u, 0.62), color: d.accent, lineHeight: 1, letterSpacing: -4 * d.u, fontVariantNumeric: 'tabular-nums', textShadow: shade}}>{text}</div>
        {p.label ? <div style={{marginTop: 24 * d.u, fontFamily: d.body, fontWeight: Math.min(d.weight, 700), fontSize: fitSize(p.label, box.w, 58 * d.u, 0.56), color: d.paper, textAlign: 'center', textShadow: shade}}>{p.label}</div> : null}
      </div>
    </div>
  );
};
