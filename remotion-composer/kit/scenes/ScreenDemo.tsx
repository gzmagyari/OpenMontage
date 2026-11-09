// A screenshot or screen recording in a browser or phone frame, with a camera that moves in
// on what matters, a cursor, clicks and highlighted areas. Every position is a fraction of the
// screen content: 0,0 is its top left corner and 1,1 its bottom right.

import React from 'react';
import {Img, OffthreadVideo, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {HEAD_START, clamp01, easeIO, surface, useDress} from '../engine/ui.tsx';
import {Backdrop, type BackdropSpec} from './Backdrop.tsx';

export type DemoStep = {
  /** Seconds into the shot. */
  at: number;
  /** Camera: how far to move in (1 = the whole screen) and which point to centre. */
  zoom?: number;
  focus?: [number, number];
  /** An area to outline: x, y, width, height. `null` removes the outline. */
  highlight?: [number, number, number, number] | null;
  /** The cursor arrives here at this moment; `click` adds a click. */
  cursor?: [number, number];
  click?: boolean;
  /** A short note shown above the screen while this step is current. */
  label?: string;
};

export type ScreenDemoProps = {
  image?: string;
  video?: string;
  frame?: 'browser' | 'phone' | 'plain';
  /** Shown in the browser bar. */
  url?: string;
  /** Width / height of the screen content (default 1.6 for a browser, 0.47 for a phone). */
  aspect?: number;
  /** Width / height of the window it is shown in. Default: the content's own shape, except a
   *  browser in a tall video, which gets a taller window that the content covers. */
  window?: number;
  steps?: DemoStep[];
  backdrop?: BackdropSpec;
};

const MOVE = 14; // frames a camera move or a cursor trip takes

export const ScreenDemo: React.FC<ScreenDemoProps> = (p) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const d = useDress();
  const kind = p.frame ?? 'browser';
  const aspect = p.aspect ?? (kind === 'phone' ? 0.47 : 1.6);
  const bar = kind === 'browser' ? 66 * d.u : 0;
  const shape = p.window ?? (kind === 'browser' && height > width ? Math.min(aspect, 1.05) : aspect);
  // Between the strip the app covers at the top and the captions, with air on both sides.
  const maxH = height * 0.5 - bar;
  const w = Math.min(width * (kind === 'phone' ? 0.6 : 0.92), maxH * shape);
  const h = w / shape;
  // The content covers the window; when it is wider, the camera pans across it.
  const cw = Math.max(w, h * aspect);
  const ch = cw / aspect;
  const steps = [...(p.steps ?? [])].sort((a, b) => a.at - b.at).map((s) => ({...s, f: Math.round(s.at * fps)}));
  const enter = spring({frame: frame + HEAD_START, fps, config: {damping: 16, stiffness: 150, mass: 0.7}});

  // Camera: glide from the previous framing to the current one.
  const shots = steps.filter((s) => s.zoom !== undefined || s.focus !== undefined);
  let cam = {s: 1, x: 0.5, y: 0.5};
  for (const step of shots) {
    if (frame < step.f) break;
    const target = {s: step.zoom ?? cam.s, x: step.focus?.[0] ?? cam.x, y: step.focus?.[1] ?? cam.y};
    // A framing given for the very start is simply where the camera begins.
    const t = step.f <= 0 ? 1 : easeIO(clamp01((frame - step.f) / MOVE));
    cam = {s: cam.s + (target.s - cam.s) * t, x: cam.x + (target.x - cam.x) * t, y: cam.y + (target.y - cam.y) * t};
  }
  // Keep the content covering its frame: no empty edge ever shows.
  const tx = Math.min(0, Math.max(w - cam.s * cw, w / 2 - cam.s * cam.x * cw));
  const ty = Math.min(0, Math.max(h - cam.s * ch, h / 2 - cam.s * cam.y * ch));

  const current = [...steps].reverse().find((s) => frame >= s.f);
  const lit = [...steps].reverse().find((s) => frame >= s.f && s.highlight !== undefined);
  const trips = steps.filter((s) => s.cursor);
  let cursor: [number, number] | null = null;
  trips.forEach((step, i) => {
    const from = i > 0 ? trips[i - 1].cursor! : ([Math.min(1, step.cursor![0] + 0.25), Math.min(1, step.cursor![1] + 0.3)] as [number, number]);
    const t = easeIO(clamp01((frame - (step.f - MOVE)) / MOVE));
    if (frame >= step.f - MOVE || (i === 0 && frame >= step.f - MOVE * 2)) cursor = [from[0] + (step.cursor![0] - from[0]) * t, from[1] + (step.cursor![1] - from[1]) * t];
  });
  const click = [...steps].reverse().find((s) => s.click && s.cursor && frame >= s.f && frame < s.f + 14);
  const src = staticFile(p.video ?? p.image ?? '');
  const media: React.CSSProperties = {position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover', objectPosition: 'top'};
  const radius = kind === 'phone' ? 58 * d.u : 22 * d.u;
  return (
    <div style={{position: 'absolute', inset: 0}}>
      <Backdrop spec={p.backdrop} />
      {current?.label ? (
        <div key={current.f} style={{position: 'absolute', left: 0, right: 0, top: height * 0.085, display: 'flex', justifyContent: 'center'}}>
          <div style={{...surface(d, {bg: d.accent}), padding: `${14 * d.u}px ${28 * d.u}px`, fontFamily: d.font, fontWeight: d.weight, fontSize: 44 * d.u, opacity: clamp01((frame - current.f) / 5)}}>{current.label}</div>
        </div>
      ) : null}
      <div
        style={{
          position: 'absolute',
          left: (width - w) / 2,
          top: height * 0.17,
          width: w,
          height: h + bar,
          borderRadius: radius,
          overflow: 'hidden',
          background: d.ink,
          border: `${(kind === 'phone' ? 14 : 5) * d.u}px solid ${kind === 'plain' ? 'transparent' : d.ink}`,
          boxShadow: `0 ${30 * d.u}px ${80 * d.u}px rgba(0,0,0,0.5)`,
          boxSizing: 'content-box',
          opacity: enter,
          transform: `translateY(${(1 - enter) * 60 * d.u}px) scale(${0.96 + 0.04 * enter})`,
        }}
      >
        {bar ? (
          <div style={{height: bar, background: d.paper, display: 'flex', alignItems: 'center', gap: 10 * d.u, padding: `0 ${20 * d.u}px`, boxSizing: 'border-box'}}>
            {[d.negative, d.accent, d.positive].map((c, i) => <span key={i} style={{width: 16 * d.u, height: 16 * d.u, borderRadius: 8 * d.u, background: c}} />)}
            {p.url ? <div style={{marginLeft: 14 * d.u, flex: 1, height: 38 * d.u, borderRadius: 19 * d.u, background: 'rgba(0,0,0,0.08)', color: d.ink, fontFamily: d.body, fontWeight: 500, fontSize: 24 * d.u, display: 'flex', alignItems: 'center', padding: `0 ${18 * d.u}px`, whiteSpace: 'nowrap', overflow: 'hidden'}}>{p.url}</div> : null}
          </div>
        ) : null}
        <div style={{position: 'relative', width: w, height: h, overflow: 'hidden'}}>
          <div style={{position: 'absolute', left: 0, top: 0, width: cw, height: ch, transform: `translate(${tx}px, ${ty}px) scale(${cam.s})`, transformOrigin: '0 0'}}>
            {p.video ? <OffthreadVideo src={src} muted volume={0} style={media} /> : <Img src={src} style={media} />}
            {lit?.highlight ? (
              <div
                style={{
                  position: 'absolute',
                  left: lit.highlight[0] * cw,
                  top: lit.highlight[1] * ch,
                  width: lit.highlight[2] * cw,
                  height: lit.highlight[3] * ch,
                  borderRadius: (12 * d.u) / cam.s,
                  border: `${(6 * d.u) / cam.s}px solid ${d.accent}`,
                  boxShadow: `0 0 0 ${(3 * d.u) / cam.s}px ${d.ink}, 0 0 ${(40 * d.u) / cam.s}px ${d.accent}`,
                  boxSizing: 'border-box',
                  opacity: clamp01((frame - lit.f) / 6),
                  transform: `scale(${interpolate(frame - lit.f, [0, 8], [1.12, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'})})`,
                }}
              />
            ) : null}
            {click ? (
              <div style={{position: 'absolute', left: click.cursor![0] * cw, top: click.cursor![1] * ch, width: 0, height: 0}}>
                <div style={{position: 'absolute', left: -60 * d.u / cam.s, top: -60 * d.u / cam.s, width: 120 * d.u / cam.s, height: 120 * d.u / cam.s, borderRadius: '50%', border: `${(6 * d.u) / cam.s}px solid ${d.accent}`, boxSizing: 'border-box', transform: `scale(${interpolate(frame - click.f, [0, 14], [0.2, 1])})`, opacity: interpolate(frame - click.f, [0, 14], [1, 0])}} />
              </div>
            ) : null}
            {cursor ? (
              <svg viewBox="0 0 24 24" style={{position: 'absolute', left: cursor[0] * cw, top: cursor[1] * ch, width: (54 * d.u) / cam.s, height: (54 * d.u) / cam.s, filter: 'drop-shadow(0 3px 5px rgba(0,0,0,0.5))'}}>
                <path d="M3 2 L3 19 L8 14.5 L11.5 22 L14.5 20.6 L11 13.5 L18 13.5 Z" fill="#fff" stroke="#111" strokeWidth="1.4" strokeLinejoin="round" />
              </svg>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  );
};
