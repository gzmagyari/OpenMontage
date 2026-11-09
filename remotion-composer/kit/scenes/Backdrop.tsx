// What sits behind a scene that fills the frame: a colour, a picture or a clip, optionally
// blurred and dimmed so text on top stays readable.

import React from 'react';
import {Img, OffthreadVideo, staticFile} from 'remotion';
import {useLook} from '../engine/context.tsx';
import {fill} from '../engine/ui.tsx';

export type BackdropSpec = string | {color?: string; image?: string; video?: string; blur?: number; dim?: number};

const IMAGE = /\.(png|jpe?g|webp|gif)$/i;
const VIDEO = /\.(mp4|mov|webm|m4v)$/i;

/** A bare string is a colour, or the path of a picture or clip. */
export const Backdrop: React.FC<{spec?: BackdropSpec}> = ({spec}) => {
  const look = useLook();
  const s = typeof spec === 'string' ? (IMAGE.test(spec) ? {image: spec} : VIDEO.test(spec) ? {video: spec} : {color: spec}) : spec ?? {};
  const media = s.image ?? s.video;
  const dim = s.dim ?? (media ? 0.45 : 0);
  const style: React.CSSProperties = {...fill, filter: s.blur ? `blur(${s.blur}px)` : undefined, transform: s.blur ? 'scale(1.15)' : undefined};
  return (
    <div style={{position: 'absolute', inset: 0, overflow: 'hidden', background: s.color ?? look.background ?? look.tokens.color.ink}}>
      {s.image ? <Img src={staticFile(s.image)} style={style} /> : null}
      {s.video ? <OffthreadVideo src={staticFile(s.video)} muted volume={0} style={style} /> : null}
      {dim > 0 ? <div style={{position: 'absolute', inset: 0, background: `rgba(0,0,0,${dim})`}} /> : null}
    </div>
  );
};
