// What a customer said: the quote, who said it, and stars when there is a rating. Fills the
// frame as a main item, or sits in a zone over footage as an overlay.

import React from 'react';
import {Img, spring, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import type {Rect} from '../engine/types.ts';
import {HEAD_START, surface, useDress} from '../engine/ui.tsx';
import {Backdrop, type BackdropSpec} from './Backdrop.tsx';

export type QuoteCardProps = {
  quote: string;
  name?: string;
  role?: string;
  /** 1 to 5. */
  stars?: number;
  /** A square picture of the person. */
  avatar?: string;
  backdrop?: BackdropSpec;
  /** Set by the engine when the card is used as an overlay. */
  zone?: Rect;
  frames?: number;
};

export const QuoteCard: React.FC<QuoteCardProps> = (p) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const d = useDress();
  const overlay = p.zone !== undefined;
  const box = p.zone ?? {x: 90 * d.u, y: height * 0.22, w: width - 180 * d.u, h: height * 0.46};
  const pop = spring({frame: frame + HEAD_START, fps, config: {damping: 14, stiffness: 150, mass: 0.7}});
  const quote = String(p.quote ?? '');
  // Longer quotes get smaller type so the card never outgrows its box.
  const size = Math.max(40, Math.min(68, 1500 / Math.sqrt(Math.max(20, quote.length)) / 2.2)) * d.u;
  const stars = Math.max(0, Math.min(5, Math.round(p.stars ?? 0)));
  return (
    <div style={{position: 'absolute', inset: 0}}>
      {overlay ? null : <Backdrop spec={p.backdrop} />}
      <div style={{position: 'absolute', left: box.x, top: box.y, width: box.w, height: box.h, display: 'flex', alignItems: 'center', justifyContent: 'center'}}>
        <div style={{...surface(d), width: '100%', padding: `${44 * d.u}px ${46 * d.u}px ${40 * d.u}px`, opacity: Math.min(1, pop * 1.5), transform: `scale(${0.92 + 0.08 * pop}) translateY(${(1 - pop) * 30 * d.u}px)`}}>
          <div style={{fontFamily: d.font, fontWeight: d.weight, fontSize: 150 * d.u, lineHeight: 0.6, color: d.accent, height: 62 * d.u}}>{'“'}</div>
          <div style={{fontFamily: d.body, fontWeight: Math.min(d.weight, 700), fontSize: size, lineHeight: 1.18, color: d.ink}}>{quote}</div>
          {stars ? <div style={{marginTop: 22 * d.u, fontSize: 46 * d.u, letterSpacing: 6 * d.u, color: d.accent, WebkitTextStroke: d.shadow === 'hard' ? `${2 * d.u}px ${d.ink}` : undefined}}>{'★'.repeat(stars)}</div> : null}
          {p.name ? (
            <div style={{marginTop: 26 * d.u, display: 'flex', alignItems: 'center', gap: 18 * d.u}}>
              {p.avatar ? <Img src={staticFile(p.avatar)} style={{width: 84 * d.u, height: 84 * d.u, borderRadius: 42 * d.u, objectFit: 'cover', border: d.border ? `${Math.max(3, d.border / 2)}px solid ${d.ink}` : undefined}} /> : null}
              <div>
                <div style={{fontFamily: d.font, fontWeight: d.weight, fontSize: 40 * d.u, color: d.ink, lineHeight: 1.1}}>{p.name}</div>
                {p.role ? <div style={{fontFamily: d.body, fontWeight: 500, fontSize: 30 * d.u, color: d.ink, opacity: 0.7, marginTop: 4 * d.u}}>{p.role}</div> : null}
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
};
