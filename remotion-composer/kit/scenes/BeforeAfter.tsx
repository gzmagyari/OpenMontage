// Before/after: two pictures of the same thing and a divider that wipes from one to the other.
// The handle and the labels are drawn by the look (parts.BeforeAfterChrome), so the scene takes
// on the job's style.

import React from 'react';
import {Easing, Img, interpolate, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {useLook, useShot} from '../engine/context.tsx';
import {clamp01, easeIO, fill} from '../engine/ui.tsx';

export type ChromeProps = {
  /** Divider position, 0 (left edge) to 1 (right edge). */
  d: number;
  afterSide: 'left' | 'right';
  labels: {before: string; after: string} | null;
  /** Share of the picture each side currently shows. */
  beforeFrac: number;
  afterFrac: number;
  labelSize: number;
  labelInset: number;
  labelTop: number;
};

const DefaultChrome: React.FC<ChromeProps> = ({d, afterSide, labels, labelSize, labelInset, labelTop}) => {
  const label = (text: string, side: 'left' | 'right') => (
    <div style={{position: 'absolute', top: labelTop, [side]: labelInset, background: 'rgba(0,0,0,0.7)', color: '#fff', fontFamily: 'DejaVu Sans, sans-serif', fontWeight: 700, fontSize: labelSize, padding: `${labelSize * 0.25}px ${labelSize * 0.5}px`, borderRadius: labelSize * 0.3}}>
      {text}
    </div>
  );
  return (
    <>
      {d > 0.015 && d < 0.985 ? <div style={{position: 'absolute', top: 0, bottom: 0, left: `${d * 100}%`, width: 6, marginLeft: -3, background: '#fff', boxShadow: '0 0 18px rgba(0,0,0,0.55)'}} /> : null}
      {labels ? label(labels.after, afterSide) : null}
      {labels ? label(labels.before, afterSide === 'left' ? 'right' : 'left') : null}
    </>
  );
};

/** The two pictures and the divider at position `d`. Use this inside your own card or scene. */
export const BeforeAfterCore: React.FC<{
  before: string;
  after: string;
  d: number;
  afterSide?: 'left' | 'right';
  objectPosition?: string;
  labels?: {before: string; after: string} | null;
  labelSize?: number;
  labelInset?: number;
  labelTop?: number;
  imageStyle?: React.CSSProperties;
}> = ({before, after, d, afterSide = 'left', objectPosition = 'center 30%', labels = null, labelSize = 40, labelInset = 24, labelTop = 24, imageStyle}) => {
  const look = useLook();
  const Chrome = (look.parts?.BeforeAfterChrome as React.FC<ChromeProps> | undefined) ?? DefaultChrome;
  const img: React.CSSProperties = {...fill, objectPosition, ...imageStyle};
  const clip = afterSide === 'left' ? `inset(0 ${(1 - d) * 100}% 0 0)` : `inset(0 0 0 ${d * 100}%)`;
  const afterFrac = afterSide === 'left' ? d : 1 - d;
  return (
    <div style={{position: 'absolute', inset: 0, overflow: 'hidden'}}>
      <Img src={staticFile(before)} style={img} />
      <div style={{position: 'absolute', inset: 0, clipPath: clip}}>
        <Img src={staticFile(after)} style={img} />
      </div>
      <Chrome d={d} afterSide={afterSide} labels={labels} beforeFrac={1 - afterFrac} afterFrac={afterFrac} labelSize={labelSize} labelInset={labelInset} labelTop={labelTop} />
    </div>
  );
};

export type BeforeAfterProps = {
  /** "refs/gran" means refs/gran_before.jpg and refs/gran_after.jpg. Or give both paths. */
  pair?: string;
  before?: string;
  after?: string;
  afterSide?: 'left' | 'right';
  labels?: {before?: string; after?: string} | false;
  objectPosition?: string;
  /** The wipe: when it starts (seconds into the shot), how long it takes, and between which positions. */
  wipe?: {start?: number; seconds?: number; maxFraction?: number; from?: number; to?: number; ease?: 'smooth' | 'cubic'};
  /** Slow zoom across the shot. */
  zoom?: [number, number];
  origin?: string;
  /** "pictures": only the two pictures zoom, the handle and labels stay put. Default: everything. */
  zoomWhat?: 'all' | 'pictures';
  /** "card": the pictures sit in a frame over their own blur (for landscape pictures in a tall video). */
  frame?: 'full' | 'card';
  aspect?: number;
  width?: number;
  top?: number;
  labelSize?: number;
  labelInset?: number;
  labelTop?: number;
};

export const BeforeAfter: React.FC<BeforeAfterProps> = (p) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const shot = useShot();
  const look = useLook();
  const before = p.before ?? `${p.pair}_before.jpg`;
  const after = p.after ?? `${p.pair}_after.jpg`;
  const afterSide = p.afterSide ?? 'left';
  const labels = p.labels === false ? null : {before: p.labels?.before ?? 'BEFORE', after: p.labels?.after ?? 'AFTER'};

  const dur = Math.max(1, shot.frames);
  const startF = Math.round((p.wipe?.start ?? 2 / 30) * fps);
  const wipeF = p.wipe?.seconds !== undefined
    ? Math.max(6, Math.min(Math.round(p.wipe.seconds * fps), Math.round(dur * (p.wipe.maxFraction ?? 0.85))))
    : Math.max(8, Math.round(dur * 0.7));
  const ease = p.wipe?.ease === 'cubic' ? Easing.inOut(Easing.cubic) : easeIO;
  const t = ease(clamp01((frame - startF) / wipeF));
  const from = p.wipe?.from ?? (afterSide === 'left' ? 0 : 1);
  const to = p.wipe?.to ?? (afterSide === 'left' ? 1 : 0);
  const d = from + (to - from) * t;

  const card = p.frame === 'card';
  const [z0, z1] = p.zoom ?? (card ? [1, 1.07] : [1, 1.1]);
  const zoom = interpolate(frame, [0, dur + 6], [z0, z1], {extrapolateRight: 'clamp'});
  const picturesOnly = p.zoomWhat === 'pictures';
  const core = (size: {labelSize: number; labelInset: number; labelTop: number}, objectPosition: string) => (
    <BeforeAfterCore
      before={before} after={after} d={d} afterSide={afterSide} labels={labels} objectPosition={p.objectPosition ?? objectPosition}
      imageStyle={picturesOnly ? {transform: `scale(${zoom})`, transformOrigin: p.origin ?? '50% 40%'} : undefined}
      {...size}
    />
  );

  if (!card) {
    return (
      <div style={{position: 'absolute', inset: 0, transform: picturesOnly ? undefined : `scale(${zoom})`, transformOrigin: p.origin ?? '50% 40%'}}>
        {core({labelSize: p.labelSize ?? width * (52 / 1080), labelInset: p.labelInset ?? width * (110 / 1080), labelTop: p.labelTop ?? height * (170 / 1920)}, 'center 25%')}
      </div>
    );
  }
  const cw = (p.width ?? 1000 / 1080) * width;
  const ch = cw / (p.aspect ?? 3 / 2);
  const Frame = look.Frame;
  const box: React.CSSProperties = {position: 'absolute', left: (width - cw) / 2, top: (p.top ?? 500 / 1920) * height, width: cw, height: ch, transform: `scale(${zoom})`};
  const inner = core({labelSize: p.labelSize ?? width * (40 / 1080), labelInset: p.labelInset ?? width * (20 / 1080), labelTop: p.labelTop ?? width * (20 / 1080)}, 'center');
  return (
    <div style={{position: 'absolute', inset: 0, background: look.background ?? '#111'}}>
      <Img src={staticFile(after)} style={{...fill, filter: 'blur(46px) brightness(0.45) saturate(1.2)', transform: 'scale(1.35)'}} />
      {Frame ? <Frame style={box}>{inner}</Frame> : <div style={{...box, borderRadius: 32, overflow: 'hidden', boxShadow: '0 30px 60px rgba(0,0,0,0.5)'}}>{inner}</div>}
    </div>
  );
};
