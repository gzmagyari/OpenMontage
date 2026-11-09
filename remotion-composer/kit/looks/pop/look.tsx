// The "pop" look's components. Everything here is yours to change for the video in hand:
// restyle through tokens.ts first, then edit or replace any component below.

import React from 'react';
import {spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {clamp01, easeIO, type CaptionProps, type CardProps, type Look, type TagProps} from '@kit/engine';
import {BeforeAfterCore, type ChromeProps} from '@kit/scenes';
import {tokens} from './tokens.ts';

const C = tokens.color;
const FONT = tokens.font.display;
const WEIGHT = tokens.font.weight;

/** Sizes are drawn for a 1080-wide frame and scale with the canvas. */
const useUnit = (): number => useVideoConfig().width / 1080;

const str = (v: unknown, fallback = ''): string => (typeof v === 'string' ? v : fallback);
const num = (v: unknown, fallback: number): number => (typeof v === 'number' ? v : fallback);

/** Chunky pill with a hard offset shadow. */
const Pill: React.FC<{text: string; bg: string; color?: string; size: number; border?: number; shadow?: number}> = ({
  text, bg, color = C.ink, size, border = 6, shadow = 8,
}) => (
  <div
    style={{
      display: 'inline-block',
      background: bg,
      color,
      fontFamily: FONT,
      fontWeight: WEIGHT,
      fontSize: size,
      lineHeight: 1,
      padding: `${size * 0.26}px ${size * 0.5}px`,
      borderRadius: 999,
      border: `${border}px solid ${C.ink}`,
      boxShadow: `${shadow}px ${shadow}px 0 ${C.ink}`,
      whiteSpace: 'nowrap',
      letterSpacing: -0.5,
    }}
  >
    {text}
  </div>
);

/** A word or two in a title, picked out in the negative colour. */
const Accented: React.FC<{text: string; accent?: string}> = ({text, accent}) => {
  const at = accent ? text.lastIndexOf(accent) : -1;
  if (at < 0 || !accent) return <>{text}</>;
  return (
    <>
      {text.slice(0, at)}
      <span style={{color: C.negative}}>{accent}</span>
      {text.slice(at + accent.length)}
    </>
  );
};

// ---- tag: pops in with overshoot, scales out at its end

const Tag: React.FC<TagProps> = ({text, props, zone, frames}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const u = useUnit();
  const size = num(props.size, Math.max(40, Math.min(76, (0.68 * zone.w) / u / (0.6 * text.length + 1)))) * u;
  const rotate = num(props.rotate, -3);
  const pin = spring({frame, fps, config: {damping: 9, stiffness: 190, mass: 0.6}});
  const pout = frame > frames ? clamp01((frame - frames) / 6) : 0;
  return (
    <div
      style={{
        position: 'absolute',
        left: 0,
        right: 0,
        top: zone.y + 50 * u,
        display: 'flex',
        justifyContent: 'center',
        transform: `scale(${pin * (1 - pout * 0.9)}) rotate(${rotate * pin}deg)`,
        opacity: 1 - pout,
      }}
    >
      <Pill text={text} bg={str(props.bg, C.accent)} size={size} border={6 * u} shadow={8 * u} />
    </div>
  );
};

// ---- text: a large statement in the middle of the frame ("\n" starts a new line)

const Text: React.FC<TagProps> = ({text, props, zone, frames}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const u = useUnit();
  const lines = text.split('\n');
  const longest = lines.reduce((a, b) => (b.length > a.length ? b : a), '');
  const size = Math.min(num(props.size, 124) * u, zone.w / (0.68 * Math.max(1, longest.length)));
  const pin = spring({frame, fps, config: {damping: 11, stiffness: 180, mass: 0.6}});
  const pout = frame > frames ? clamp01((frame - frames) / 6) : 0;
  return (
    <div style={{position: 'absolute', left: zone.x, top: zone.y, width: zone.w, height: zone.h, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', transform: `scale(${(0.8 + 0.2 * pin) * (1 - pout * 0.4)})`, opacity: Math.min(1, pin * 1.5) * (1 - pout)}}>
      {lines.map((line, i) => (
        <div key={i} style={{fontFamily: FONT, fontWeight: WEIGHT, fontSize: size, lineHeight: 1.08, color: str(props.color, C.paper), WebkitTextStroke: `${Math.round(size * 0.13)}px ${C.ink}`, paintOrder: 'stroke fill', textShadow: `0 ${size * 0.07}px 0 ${C.ink}, 0 ${size * 0.14}px ${size * 0.3}px rgba(0,0,0,0.55)`, letterSpacing: -1, textAlign: 'center', whiteSpace: 'nowrap'}}>
          {line}
        </div>
      ))}
    </div>
  );
};

// ---- captions: a short phrase, the spoken word in the accent colour with a small pop

const Caption: React.FC<CaptionProps> = ({group, active, localFrame, frame, zone}) => {
  const {fps, height} = useVideoConfig();
  const u = useUnit();
  const maxChars = Math.max(...group.words.map((w) => w.text.length));
  const fs = Math.min(tokens.caption.maxSize * u, zone.w / (0.74 * maxChars));
  const pop = spring({frame: localFrame, fps, config: {damping: 14, stiffness: 260, mass: 0.5}});
  return (
    <div
      style={{
        position: 'absolute',
        left: zone.x,
        width: zone.w,
        bottom: height - (zone.y + zone.h),
        display: 'flex',
        flexWrap: 'wrap',
        justifyContent: 'center',
        alignItems: 'flex-end',
        columnGap: group.spaced ? fs * 0.5 : 0,
        rowGap: fs * 0.04,
        direction: group.rtl ? 'rtl' : 'ltr',
        transform: `scale(${0.9 + 0.1 * pop})`,
        transformOrigin: '50% 100%',
        opacity: Math.min(1, pop * 1.5),
      }}
    >
      {group.words.map((w, i) => {
        const isActive = i === active;
        const wp = isActive ? spring({frame: frame - w.from, fps, config: {damping: 10, stiffness: 320, mass: 0.4}}) : 1;
        return (
          <span
            key={i}
            style={{
              display: 'inline-block',
              fontFamily: FONT,
              fontWeight: WEIGHT,
              fontSize: fs,
              lineHeight: 1.08,
              color: isActive ? C.accent : C.paper,
              WebkitTextStroke: `${Math.round(fs * 0.13)}px ${C.ink}`,
              paintOrder: 'stroke fill',
              textShadow: `0 ${fs * 0.07}px 0 ${C.ink}, 0 ${fs * 0.14}px ${fs * 0.3}px rgba(0,0,0,0.55)`,
              transform: `scale(${isActive ? 1 + 0.08 * Math.min(1, wp) : 1}) translateY(${isActive ? -3 * wp : 0}px)`,
              transformOrigin: '50% 80%',
              letterSpacing: -1,
            }}
          >
            {w.text}
          </span>
        );
      })}
    </div>
  );
};

// ---- cards

const offsetShadow = (u: number, deep: string) =>
  `${16 * u}px ${16 * u}px 0 ${C.accent}, ${16 * u}px ${16 * u}px 0 ${3 * u}px ${C.ink}, ${deep}`;

/** Hook: a before/after picture in a floating frame over the presenter, wiping as the video opens.
 *  props: pair (or before + after), labels {before, after}. */
const HookCard: React.FC<CardProps> = ({props, zone, frames}) => {
  const frame = useCurrentFrame();
  const {fps, width} = useVideoConfig();
  const u = useUnit();
  const wipe = easeIO(clamp01(frame / 36));
  const d = 0.72 * (1 - wipe);
  const float = Math.sin(frame / 20);
  const exitStart = frames - 7;
  const ex = frame >= exitStart ? spring({frame: frame - exitStart, fps, durationInFrames: 7, config: {damping: 14, stiffness: 200, mass: 0.5}}) : 0;
  const w = 640 * u;
  const h = 480 * u;
  const labels = props.labels as {before?: string; after?: string} | undefined;
  return (
    <div
      style={{
        position: 'absolute',
        left: (width - w) / 2,
        top: zone.y + 5 * u,
        width: w,
        height: h,
        transform: `translateY(${(float * 7 - ex * 520) * u}px) scale(${(1 + 0.012 * float) * (1 - ex * 0.2)}) rotate(${-1.5 + float * 0.6 + ex * -6}deg)`,
        opacity: 1 - ex,
        borderRadius: 40 * u,
        border: `${tokens.shape.border * u}px solid ${C.ink}`,
        boxShadow: offsetShadow(u, '0 30px 60px rgba(0,0,0,0.35)'),
        overflow: 'hidden',
        background: C.ink,
      }}
    >
      <BeforeAfterCore
        before={str(props.before, `${str(props.pair)}_before.jpg`)}
        after={str(props.after, `${str(props.pair)}_after.jpg`)}
        d={d}
        afterSide="right"
        objectPosition={str(props.objectPosition, 'center 22%')}
        labels={{before: labels?.before ?? 'BEFORE', after: labels?.after ?? 'AFTER'}}
        labelSize={44 * u}
        labelInset={20 * u}
        labelTop={20 * u}
      />
    </div>
  );
};

/** Call to action: slides into the upper third. props: title, accent (words of the title to pick out), button. */
const CtaCard: React.FC<CardProps> = ({props, zone, frames}) => {
  const frame = useCurrentFrame();
  const {fps, width} = useVideoConfig();
  const u = useUnit();
  const p = spring({frame, fps, config: {damping: 11, stiffness: 170, mass: 0.7}});
  const exitStart = frames - 7;
  const ex = frame >= exitStart ? spring({frame: frame - exitStart, fps, durationInFrames: 7, config: {damping: 16, stiffness: 220, mass: 0.5}}) : 0;
  const title = str(props.title);
  const cardW = width - 180 * u;
  // One line: DejaVu Sans Bold averages 0.71 em per character.
  const size = Math.min(80 * u, (cardW - 56 * u) / (0.71 * Math.max(6, title.length)));
  return (
    <div
      style={{
        position: 'absolute',
        left: 90 * u,
        width: cardW,
        top: zone.y + 50 * u,
        boxSizing: 'border-box',
        transform: `translateY(${((1 - p) * -520 - ex * 400) * u}px) rotate(${(1 - p) * -4}deg) scale(${1 - ex * 0.1})`,
        opacity: 1 - ex,
        background: C.accent,
        border: `${tokens.shape.border * u}px solid ${C.ink}`,
        borderRadius: tokens.shape.radius * u,
        boxShadow: `${tokens.shape.offset * u}px ${tokens.shape.offset * u}px 0 ${C.ink}`,
        padding: `${40 * u}px ${20 * u}px ${38 * u}px`,
        textAlign: 'center',
      }}
    >
      <div style={{fontFamily: FONT, fontWeight: WEIGHT, fontSize: size, color: C.ink, letterSpacing: -2 * u, lineHeight: 1, whiteSpace: 'nowrap'}}>
        <Accented text={title} accent={str(props.accent) || undefined} />
      </div>
      {props.button ? (
        <div style={{marginTop: 26 * u, display: 'flex', justifyContent: 'center'}}>
          <Pill text={str(props.button)} bg={C.ink} color={C.paper} size={38 * u} border={0} shadow={0} />
        </div>
      ) : null}
    </div>
  );
};

/** End card: dims the frame and lands the address. props: title, accent, badge. */
const EndCard: React.FC<CardProps> = ({props}) => {
  const frame = useCurrentFrame();
  const {fps, width} = useVideoConfig();
  const u = useUnit();
  const dim = clamp01(frame / 8) * 0.68;
  const p = spring({frame, fps, config: {damping: 13, stiffness: 150, mass: 0.8}});
  const drift = 1 + 0.02 * clamp01(frame / (fps * 1.6));
  const title = str(props.title);
  const cardW = width - 150 * u;
  const size = Math.min(66 * u, (cardW - 56 * u) / (0.66 * Math.max(6, title.length)));
  return (
    <div style={{position: 'absolute', inset: 0}}>
      <div style={{position: 'absolute', inset: 0, background: `rgba(18,18,18,${dim})`}} />
      <div style={{position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center'}}>
        <div
          style={{
            transform: `scale(${(0.82 + 0.18 * p) * drift})`,
            opacity: clamp01(p * 1.6),
            width: cardW,
            boxSizing: 'border-box',
            background: C.paper,
            border: `${tokens.shape.border * u}px solid ${C.ink}`,
            borderRadius: 48 * u,
            boxShadow: offsetShadow(u, '0 40px 80px rgba(0,0,0,0.5)'),
            padding: `${64 * u}px ${20 * u}px ${56 * u}px`,
            textAlign: 'center',
          }}
        >
          <div style={{fontFamily: FONT, fontWeight: WEIGHT, fontSize: size, color: C.ink, letterSpacing: -2.5 * u, lineHeight: 1, whiteSpace: 'nowrap'}}>
            <Accented text={title} accent={str(props.accent) || undefined} />
          </div>
          {props.badge ? (
            <div style={{marginTop: 38 * u, display: 'flex', justifyContent: 'center'}}>
              <Pill text={str(props.badge)} bg={C.positive} color={C.paper} size={44 * u} border={6 * u} shadow={8 * u} />
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
};

// ---- pieces of ready-made scenes

/** Before/after: a white divider, a round accent handle, and a label on each side that
 *  fades in once its side shows. */
const BeforeAfterChrome: React.FC<ChromeProps> = ({d, afterSide, labels, beforeFrac, afterFrac, labelSize, labelInset, labelTop}) => {
  const u = useUnit();
  const beforeSide = afterSide === 'left' ? 'right' : 'left';
  const labelStyle = (side: 'left' | 'right', frac: number): React.CSSProperties => ({
    position: 'absolute',
    top: labelTop,
    [side]: labelInset,
    opacity: clamp01((frac - 0.1) / 0.15),
    transform: `scale(${0.85 + 0.15 * clamp01((frac - 0.1) / 0.15)})`,
  });
  const pill = (text: string, bg: string) => (
    <Pill text={text} bg={bg} color={C.paper} size={labelSize} border={Math.max(4 * u, labelSize / 8)} shadow={Math.max(4 * u, labelSize / 6)} />
  );
  return (
    <>
      {d > 0.015 && d < 0.985 ? (
        <>
          <div style={{position: 'absolute', top: 0, bottom: 0, left: `${d * 100}%`, width: 6 * u, marginLeft: -3 * u, background: C.paper, boxShadow: '0 0 18px rgba(0,0,0,0.55)'}} />
          <div
            style={{
              position: 'absolute',
              top: '50%',
              left: `${d * 100}%`,
              width: 64 * u,
              height: 64 * u,
              marginLeft: -32 * u,
              marginTop: -32 * u,
              borderRadius: 32 * u,
              background: C.accent,
              border: `${5 * u}px solid ${C.ink}`,
              boxShadow: `${4 * u}px ${4 * u}px 0 ${C.ink}`,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              padding: `0 ${11 * u}px`,
              boxSizing: 'border-box',
            }}
          >
            <div style={{borderTop: `${9 * u}px solid transparent`, borderBottom: `${9 * u}px solid transparent`, borderRight: `${10 * u}px solid ${C.ink}`}} />
            <div style={{borderTop: `${9 * u}px solid transparent`, borderBottom: `${9 * u}px solid transparent`, borderLeft: `${10 * u}px solid ${C.ink}`}} />
          </div>
        </>
      ) : null}
      {labels ? <div style={labelStyle(beforeSide, beforeFrac)}>{pill(labels.before, C.negative)}</div> : null}
      {labels ? <div style={labelStyle(afterSide, afterFrac)}>{pill(labels.after, C.positive)}</div> : null}
    </>
  );
};

/** The frame around media in card and picture-in-picture layouts. */
const Frame: React.FC<{children: React.ReactNode; style?: React.CSSProperties}> = ({children, style}) => {
  const u = useUnit();
  return (
    <div style={{borderRadius: 36 * u, border: `${tokens.shape.border * u}px solid ${C.ink}`, boxShadow: offsetShadow(u, '0 30px 60px rgba(0,0,0,0.5)'), overflow: 'hidden', ...style}}>
      {children}
    </div>
  );
};

const Vignette: React.FC = () => (
  <div style={{position: 'absolute', inset: 0, pointerEvents: 'none', background: 'radial-gradient(ellipse at 50% 45%, rgba(0,0,0,0) 55%, rgba(18,18,18,0.38) 100%)'}} />
);

export const look: Look = {
  name: 'pop',
  tokens,
  background: C.ink,
  Caption,
  Tag,
  Text,
  cards: {hook: HookCard, cta: CtaCard, end: EndCard},
  cardZones: {hook: 'upper', cta: 'upper', end: 'full'},
  Finish: Vignette,
  Frame,
  parts: {BeforeAfterChrome},
};
