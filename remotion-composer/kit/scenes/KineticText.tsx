// Big words that arrive one after another: a statement that needs no footage.

import React from 'react';
import {spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {HEAD_START, fitSize, useDress} from '../engine/ui.tsx';
import {Backdrop, type BackdropSpec} from './Backdrop.tsx';

export type KineticTextProps = {
  /** The statement; "\n" starts a new line. Or give `lines`. */
  text?: string;
  lines?: string[];
  /** Words to draw in the accent colour. */
  emphasis?: string | string[];
  backdrop?: BackdropSpec;
  align?: 'center' | 'left';
  /** Largest font size, for a 1080-wide frame (default 150). */
  size?: number;
  /** Frames between one word and the next (default 3). */
  stagger?: number;
  uppercase?: boolean;
  color?: string;
};

const fold = (s: string): string => s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');

export const KineticText: React.FC<KineticTextProps> = (p) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const d = useDress();
  const lines = (p.lines ?? String(p.text ?? '').split('\n')).map((l) => (p.uppercase ? l.toUpperCase() : l)).filter((l) => l.trim());
  const strong = new Set([p.emphasis ?? []].flat().flatMap((e) => e.split(/\s+/)).map(fold));
  const usable = width - 180 * d.u;
  const longest = lines.reduce((a, b) => (b.length > a.length ? b : a), '');
  const size = Math.min((p.size ?? 150) * d.u, fitSize(longest, usable, 400 * d.u, 0.62), (height * 0.5) / Math.max(1, lines.length) / 1.08);
  const stagger = p.stagger ?? 3;
  let index = 0;
  return (
    <div style={{position: 'absolute', inset: 0}}>
      <Backdrop spec={p.backdrop} />
      <div style={{position: 'absolute', left: 90 * d.u, width: usable, top: 0, bottom: height * 0.08, display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: p.align === 'left' ? 'flex-start' : 'center'}}>
        {lines.map((line, li) => (
          <div key={li} style={{display: 'flex', flexWrap: 'wrap', justifyContent: p.align === 'left' ? 'flex-start' : 'center', columnGap: size * 0.26, fontFamily: d.font, fontWeight: d.weight, fontSize: size, lineHeight: 1.08, letterSpacing: -size * 0.02}}>
            {line.split(/\s+/).filter(Boolean).map((word, wi) => {
              const pop = spring({frame: frame + HEAD_START - index++ * stagger, fps, config: {damping: 13, stiffness: 200, mass: 0.6}});
              return (
                <span key={wi} style={{display: 'inline-block', color: strong.has(fold(word)) ? d.accent : p.color ?? d.paper, opacity: Math.min(1, pop * 1.4), transform: `translateY(${(1 - pop) * 44 * d.u}px) scale(${0.9 + 0.1 * pop})`, textShadow: p.backdrop ? '0 4px 18px rgba(0,0,0,0.45)' : undefined}}>
                  {word}
                </span>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
};
