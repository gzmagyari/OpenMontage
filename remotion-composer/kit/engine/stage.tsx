// What fills the frame: entrances, clips and layouts.

import React from 'react';
import {Freeze, Img, OffthreadVideo, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {ShotContext, useKit, useShot} from './context.tsx';
import type {ResolvedClip, ResolvedEnter, ResolvedLayout} from './types.ts';
import {Motion, clamp01, fill} from './ui.tsx';

const ENTER_FEEL = {damping: 18, stiffness: 230, mass: 0.55};

/** The incoming shot arrives on top of the outgoing one, so no cut ever moves in time. */
export const Enter: React.FC<{enter: ResolvedEnter; children: React.ReactNode}> = ({enter, children}) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const {look} = useKit();
  const box: React.CSSProperties = {position: 'absolute', inset: 0, overflow: 'hidden'};
  if (enter.type === 'none' || enter.frames <= 0) return <div style={box}>{children}</div>;
  const p = spring({frame, fps, durationInFrames: enter.frames, config: look.motion?.enter ?? ENTER_FEEL});
  const q = 1 - p;
  let transform = 'none';
  let opacity = 1;
  let shadow = 'none';
  let clipPath: string | undefined;
  let blur = q > 0.02 ? `blur(${q * 12}px)` : undefined;
  switch (enter.type) {
    case 'slide-left':
      transform = `translateX(${q * width}px)`;
      shadow = '-40px 0 70px rgba(0,0,0,0.35)';
      break;
    case 'slide-right':
      transform = `translateX(${-q * width}px)`;
      shadow = '40px 0 70px rgba(0,0,0,0.35)';
      break;
    case 'slide-up':
      transform = `translateY(${q * height}px)`;
      shadow = '0 -40px 70px rgba(0,0,0,0.35)';
      break;
    case 'slide-down':
      transform = `translateY(${-q * height}px)`;
      shadow = '0 40px 70px rgba(0,0,0,0.35)';
      break;
    case 'whip':
      transform = `translateX(${q * width}px) skewX(${-q * 8}deg)`;
      shadow = '-40px 0 70px rgba(0,0,0,0.35)';
      break;
    case 'punch':
      transform = `scale(${1 + q * 0.28})`;
      opacity = clamp01(frame / Math.max(2, enter.frames * 0.5));
      break;
    case 'fade':
      opacity = interpolate(frame, [0, enter.frames], [0, 1], {extrapolateRight: 'clamp'});
      blur = undefined;
      break;
    case 'wipe':
      clipPath = `inset(0 ${interpolate(frame, [0, enter.frames], [100, 0], {extrapolateRight: 'clamp'})}% 0 0)`;
      blur = undefined;
      break;
    case 'flash':
      blur = undefined;
      break;
  }
  return (
    <div style={{...box, transform, opacity, boxShadow: shadow, clipPath, filter: blur}}>
      {children}
      {enter.type === 'flash' ? (
        <div style={{position: 'absolute', inset: 0, background: '#fff', opacity: interpolate(frame, [0, enter.frames], [0.9, 0], {extrapolateRight: 'clamp'})}} />
      ) : null}
    </div>
  );
};

/** Stands in for a file that is still being generated, so layout can be judged before it lands. */
export const Placeholder: React.FC<{label: string}> = ({label}) => {
  const {width} = useVideoConfig();
  return (
    <div style={{position: 'absolute', inset: 0, background: 'repeating-linear-gradient(45deg, #2a2d34, #2a2d34 40px, #32363f 40px, #32363f 80px)', display: 'flex', alignItems: 'center', justifyContent: 'center'}}>
      <div style={{fontFamily: 'DejaVu Sans, sans-serif', fontSize: width * 0.034, color: '#cfd3da', background: 'rgba(0,0,0,0.55)', padding: '18px 28px', borderRadius: 14, maxWidth: '80%', textAlign: 'center', wordBreak: 'break-all'}}>
        waiting for {label}
      </div>
    </div>
  );
};

const Media: React.FC<{clip: ResolvedClip}> = ({clip}) => {
  const src = staticFile(clip.src ?? '');
  const style: React.CSSProperties = {...fill, objectFit: clip.fit === 'cover' ? 'cover' : 'contain'};
  const el = (extra?: React.CSSProperties) => {
    const s = {...style, ...extra};
    if (clip.kind === 'image') return <Img src={src} style={s} />;
    if (clip.kind === 'freeze') {
      return (
        <Freeze frame={0}>
          <OffthreadVideo src={src} muted volume={0} trimBefore={clip.freezeFrame ?? 0} style={s} />
        </Freeze>
      );
    }
    return <OffthreadVideo src={src} muted={clip.volume <= 0} volume={clip.volume} trimBefore={clip.trimBefore ?? 0} playbackRate={clip.speed} style={s} />;
  };
  if (clip.fit !== 'blur') return el();
  return (
    <>
      {el({objectFit: 'cover', filter: 'blur(46px) brightness(0.45) saturate(1.2)', transform: 'scale(1.35)'})}
      {el({objectFit: 'contain'})}
    </>
  );
};

const SceneView: React.FC<{clip: ResolvedClip}> = ({clip}) => {
  const {scenes} = useKit();
  const Scene = scenes[clip.scene ?? ''];
  if (!Scene) {
    // Reported to the kit command through the browser log; the frame shows it too.
    console.log(`KITDIAG ${JSON.stringify({error: 'scene-unknown', scene: clip.scene, known: Object.keys(scenes)})}`);
    return <Placeholder label={`scene "${clip.scene}" (not registered in src/scenes.ts)`} />;
  }
  return <Scene {...(clip.props ?? {})} />;
};

const DefaultFrame: React.FC<{children: React.ReactNode; style?: React.CSSProperties}> = ({children, style}) => (
  <div style={{borderRadius: 32, overflow: 'hidden', boxShadow: '0 30px 60px rgba(0,0,0,0.45)', ...style}}>{children}</div>
);

const LayoutView: React.FC<{layout: ResolvedLayout; frames: number}> = ({layout, frames}) => {
  const {width, height} = useVideoConfig();
  const {look} = useKit();
  const Frame = look.Frame ?? DefaultFrame;
  const cell = (clip: ResolvedClip, style: React.CSSProperties, key?: number) => (
    <div key={key} style={{position: 'absolute', overflow: 'hidden', ...style}}>
      <ClipView clip={clip} frames={frames} />
    </div>
  );
  if (layout.type === 'split') {
    const r = layout.ratio;
    const vertical = layout.direction === 'vertical';
    return (
      <>
        {layout.slots.slice(0, 2).map((clip, i) =>
          cell(
            clip,
            vertical
              ? {left: 0, width: '100%', top: i === 0 ? 0 : `${r * 100}%`, height: i === 0 ? `${r * 100}%` : `${(1 - r) * 100}%`}
              : {top: 0, height: '100%', left: i === 0 ? 0 : `${r * 100}%`, width: i === 0 ? `${r * 100}%` : `${(1 - r) * 100}%`},
            i,
          ),
        )}
      </>
    );
  }
  if (layout.type === 'grid') {
    const cols = Math.max(1, layout.columns);
    const rows = Math.ceil(layout.slots.length / cols);
    return (
      <>
        {layout.slots.map((clip, i) =>
          cell(clip, {left: `${((i % cols) / cols) * 100}%`, top: `${(Math.floor(i / cols) / rows) * 100}%`, width: `${100 / cols}%`, height: `${100 / rows}%`}, i),
        )}
      </>
    );
  }
  if (layout.type === 'pip') {
    const w = width * layout.size;
    const h = w * (height / width);
    const margin = width * 0.045;
    const [v, hz] = layout.corner.split('-');
    const pos: React.CSSProperties = {
      position: 'absolute', width: w, height: h,
      ...(v === 'top' ? {top: height * 0.085} : {bottom: height * 0.2}),
      ...(hz === 'right' ? {right: margin + width * 0.1} : {left: margin}),
    };
    return (
      <>
        {cell(layout.base, {inset: 0})}
        <Frame style={pos}>
          <div style={{position: 'absolute', inset: 0}}><ClipView clip={layout.inset} frames={frames} /></div>
        </Frame>
      </>
    );
  }
  // card: media in a frame, over a backdrop (its own blurred copy unless one is given)
  const cw = width * layout.width;
  const ch = cw / layout.aspect;
  const top = layout.top !== null ? layout.top * height : (height - ch) / 2;
  const backdrop = layout.backdrop ?? {...layout.media, motion: null, fit: 'cover' as const};
  return (
    <>
      <div style={{position: 'absolute', inset: 0, overflow: 'hidden', filter: layout.backdrop ? undefined : 'blur(46px) brightness(0.45) saturate(1.2)', transform: layout.backdrop ? undefined : 'scale(1.35)'}}>
        <ClipView clip={backdrop} frames={frames} />
      </div>
      <Frame style={{position: 'absolute', left: (width - cw) / 2, top, width: cw, height: ch}}>
        <div style={{position: 'absolute', inset: 0}}><ClipView clip={layout.media} frames={frames} /></div>
      </Frame>
    </>
  );
};

/** One clip: its camera move and whatever it shows. */
export const ClipView: React.FC<{clip: ResolvedClip; frames: number}> = ({clip, frames}) => {
  let body: React.ReactNode;
  if (clip.kind === 'layout' && clip.layout) body = <LayoutView layout={clip.layout} frames={frames} />;
  else if (clip.missing) body = <Placeholder label={clip.src ?? clip.kind} />;
  else if (clip.kind === 'scene') body = <SceneView clip={clip} />;
  else if (clip.kind === 'color') body = <div style={{position: 'absolute', inset: 0, background: clip.color}} />;
  else body = <Media clip={clip} />;
  return <Motion motion={clip.motion} frames={frames}>{body}</Motion>;
};

export {ShotContext, useShot};
