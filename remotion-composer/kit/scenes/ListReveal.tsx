// A short list whose points arrive one by one: reasons, steps, features. Fills the frame as a
// main item, or sits in a zone over footage as an overlay.

import React from 'react';
import {spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {useKit, useShot, wordFrame} from '../engine/context.tsx';
import type {Rect} from '../engine/types.ts';
import {HEAD_START, fitSize, surface, useDress} from '../engine/ui.tsx';
import {Backdrop, type BackdropSpec} from './Backdrop.tsx';

export type ListRevealProps = {
  title?: string;
  items: string[];
  /** When each point arrives: seconds into the shot, or a word the voice says. Default: evenly spread. */
  at?: Array<number | string>;
  numbered?: boolean;
  backdrop?: BackdropSpec;
  /** Set by the engine when the list is used as an overlay. */
  zone?: Rect;
  frames?: number;
};

export const ListReveal: React.FC<ListRevealProps> = (p) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const {resolved} = useKit();
  const shot = useShot();
  const d = useDress();
  const overlay = p.zone !== undefined;
  const total = p.frames ?? shot.frames;
  const items = p.items ?? [];
  const box = p.zone ?? {x: 90 * d.u, y: height * 0.2, w: width - 180 * d.u, h: height * 0.5};
  const starts = items.map((_, i) => {
    const when = p.at?.[i];
    if (typeof when === 'number') return Math.round(when * fps);
    if (typeof when === 'string') {
      const spoken = wordFrame(resolved, shot.from, when);
      if (spoken !== null) return spoken;
    }
    return Math.round(total * (0.1 + (0.62 * i) / Math.max(1, items.length)));
  });
  const badge = 84 * d.u;
  const longest = items.reduce((a, b) => (b.length > a.length ? b : a), '');
  const size = Math.max(34 * d.u, fitSize(longest, box.w - badge - 70 * d.u, 60 * d.u, 0.58));
  const titleIn = spring({frame: frame + HEAD_START, fps, config: {damping: 15, stiffness: 160}});
  return (
    <div style={{position: 'absolute', inset: 0}}>
      {overlay ? null : <Backdrop spec={p.backdrop} />}
      <div style={{position: 'absolute', left: box.x, top: box.y, width: box.w, height: box.h, display: 'flex', flexDirection: 'column', justifyContent: 'center', gap: 22 * d.u}}>
        {p.title ? (
          <div style={{fontFamily: d.font, fontWeight: d.weight, fontSize: fitSize(p.title, box.w, 84 * d.u, 0.6), color: overlay ? d.ink : d.paper, lineHeight: 1.05, marginBottom: 14 * d.u, opacity: titleIn, transform: `translateY(${(1 - titleIn) * 24 * d.u}px)`, ...(overlay ? {...surface(d), padding: `${16 * d.u}px ${26 * d.u}px`, alignSelf: 'flex-start'} : {})}}>
            {p.title}
          </div>
        ) : null}
        {items.map((item, i) => {
          if (frame < starts[i]) return <div key={i} style={{height: badge}} />;
          const pop = spring({frame: frame - starts[i], fps, config: {damping: 14, stiffness: 190, mass: 0.6}});
          return (
            <div key={i} style={{display: 'flex', alignItems: 'center', gap: 20 * d.u, minHeight: badge, opacity: Math.min(1, pop * 1.5), transform: `translateX(${(1 - pop) * -60 * d.u}px)`}}>
              {p.numbered === false ? null : (
                <div style={{...surface(d, {bg: d.accent, radius: badge / 2}), width: badge, height: badge, flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', fontFamily: d.font, fontWeight: d.weight, fontSize: badge * 0.52}}>
                  {i + 1}
                </div>
              )}
              <div style={{...surface(d), padding: `${18 * d.u}px ${28 * d.u}px`, fontFamily: d.font, fontWeight: d.weight, fontSize: size, lineHeight: 1.1}}>{item}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
