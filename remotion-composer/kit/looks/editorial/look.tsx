// The "editorial" look's components. Everything here is yours to change for the video in hand:
// restyle through tokens.ts first, then edit or replace any component below.

import React from 'react';
import {Easing, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {clamp01, fonts, type CaptionProps, type CardProps, type Look, type TagProps} from '@kit/engine';
import type {ChromeProps} from '@kit/scenes';
import {tokens} from './tokens.ts';

const C = tokens.color;
const FONT = tokens.font.display;
// One variable file covers every weight the look uses.
fonts.spaceGrotesk(['300 700']);

/** Sizes are drawn for a 1080-wide frame and scale with the canvas. */
const useUnit = (): number => useVideoConfig().width / 1080;
const str = (v: unknown, fallback = ''): string => (typeof v === 'string' ? v : fallback);

/** A paper-white label with an optional coloured dot. Use it in your own scenes too. */
export const Chip: React.FC<{label: string; dot?: string; style?: React.CSSProperties; delay?: number}> = ({label, dot, style, delay = 0}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const u = useUnit();
  const p = spring({frame: frame - delay, fps, config: {damping: 16, stiffness: 180}});
  return (
    <div
      style={{
        position: 'absolute',
        display: 'flex',
        alignItems: 'center',
        gap: 14 * u,
        padding: `${14 * u}px ${26 * u}px`,
        borderRadius: 999,
        background: 'rgba(245,242,235,0.94)',
        color: C.ink,
        fontFamily: FONT,
        fontWeight: 700,
        fontSize: 38 * u,
        letterSpacing: 2 * u,
        textTransform: 'uppercase',
        whiteSpace: 'nowrap',
        boxShadow: '0 8px 28px rgba(0,0,0,0.28)',
        opacity: delay > 0 ? p : 1,
        transform: delay > 0 ? `translateY(${(1 - p) * 18 * u}px)` : undefined,
        ...style,
      }}
    >
      {dot ? <span style={{width: 16 * u, height: 16 * u, borderRadius: 8 * u, background: dot}} /> : null}
      {label}
    </div>
  );
};

// ---- tag: a chip that settles into the upper third

const Tag: React.FC<TagProps> = ({text, props, zone, frames}) => {
  const frame = useCurrentFrame();
  const u = useUnit();
  const out = frame > frames ? clamp01((frame - frames) / 6) : 0;
  return (
    <div style={{position: 'absolute', inset: 0, opacity: 1 - out}}>
      <Chip label={text} dot={str(props.dot, C.accent)} delay={4} style={{left: '50%', top: zone.y + 50 * u, transform: 'translateX(-50%)'}} />
    </div>
  );
};

// ---- text: a large statement on a dark plate ("\n" starts a new line)

const Text: React.FC<TagProps> = ({text, props, zone, frames}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const u = useUnit();
  const lines = text.split('\n');
  const longest = lines.reduce((a, b) => (b.length > a.length ? b : a), '');
  const size = Math.min((typeof props.size === 'number' ? props.size : 104) * u, (zone.w - 80 * u) / (0.56 * Math.max(1, longest.length)));
  const rise = spring({frame, fps, config: {damping: 18, stiffness: 150}});
  const out = frame > frames ? clamp01((frame - frames) / 6) : 0;
  return (
    <div style={{position: 'absolute', left: zone.x, top: zone.y, width: zone.w, height: zone.h, display: 'flex', alignItems: 'center', justifyContent: 'center', opacity: rise * (1 - out), transform: `translateY(${(1 - rise) * 30 * u}px)`}}>
      <div style={{padding: `${26 * u}px ${40 * u}px`, borderRadius: 28 * u, background: 'rgba(13,13,15,0.62)', fontFamily: FONT, fontWeight: 700, fontSize: size, lineHeight: 1.12, letterSpacing: -1.5 * u, color: str(props.color, C.paper), textAlign: 'center', whiteSpace: 'nowrap'}}>
        {lines.map((line, i) => <div key={i}>{line}</div>)}
      </div>
    </div>
  );
};

// ---- captions: a phrase on a dark plate; spoken words light up, the current one in the accent colour

const Caption: React.FC<CaptionProps> = ({group, active, localFrame, frame, zone}) => {
  const u = useUnit();
  const pop = interpolate(localFrame, [0, 4], [0.9, 1], {extrapolateRight: 'clamp', easing: Easing.out(Easing.back(2))});
  return (
    <div style={{position: 'absolute', inset: 0, display: 'flex', justifyContent: 'center'}}>
      <div
        style={{
          position: 'absolute',
          top: zone.y + 180 * u,
          maxWidth: 960 * u,
          display: 'flex',
          flexWrap: 'wrap',
          justifyContent: 'center',
          gap: group.spaced ? `0 ${22 * u}px` : 0,
          direction: group.rtl ? 'rtl' : 'ltr',
          padding: `${18 * u}px ${34 * u}px`,
          borderRadius: 28 * u,
          background: 'rgba(13,13,15,0.62)',
          transform: `scale(${pop})`,
          fontFamily: FONT,
          fontWeight: 700,
          fontSize: tokens.caption.size * u,
          lineHeight: 1.15,
          letterSpacing: -1 * u,
        }}
      >
        {group.words.map((w, i) => (
          <span key={i} style={{color: i === active ? C.accent : C.paper, opacity: frame >= w.from ? 1 : 0.55, textShadow: '0 3px 12px rgba(0,0,0,0.45)'}}>
            {w.text}
          </span>
        ))}
      </div>
    </div>
  );
};

// ---- cards

/** End card: the lower half darkens and the name, a line, the button and the address rise in turn.
 *  props: title, accent (part of the title in the accent colour), sub, button, domain. */
const EndCard: React.FC<CardProps> = ({props}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const u = useUnit();
  const s = (sec: number) => Math.round(sec * fps);
  const shade = interpolate(frame, [0, s(0.5)], [0, 1], {extrapolateRight: 'clamp'});
  const rise = (d: number) => spring({frame: frame - d, fps, config: {damping: 15, stiffness: 140}});
  const item = (d: number): React.CSSProperties => ({opacity: rise(d), transform: `translateY(${(1 - rise(d)) * 40 * u}px)`});
  const pulse = 1 + 0.025 * Math.sin((frame - s(1.4)) / 6) * (frame > s(1.4) ? 1 : 0);
  const title = str(props.title);
  const accent = str(props.accent);
  const at = accent ? title.lastIndexOf(accent) : -1;
  return (
    <div style={{position: 'absolute', inset: 0}}>
      <div style={{position: 'absolute', inset: 0, background: `linear-gradient(180deg, rgba(13,13,15,0) 38%, rgba(13,13,15,${0.82 * shade}) 62%, rgba(13,13,15,${0.94 * shade}) 100%)`}} />
      <div style={{position: 'absolute', inset: 0, display: 'flex', justifyContent: 'center', fontFamily: FONT, color: C.paper}}>
        <div style={{position: 'absolute', top: 1150 * u, fontSize: 124 * u, fontWeight: 700, letterSpacing: -3 * u, whiteSpace: 'nowrap', ...item(s(0.25))}}>
          {at >= 0 ? (
            <>
              {title.slice(0, at)}
              <span style={{color: C.accent}}>{accent}</span>
              {title.slice(at + accent.length)}
            </>
          ) : title}
        </div>
        {props.sub ? (
          <div style={{position: 'absolute', top: 1310 * u, fontSize: 42 * u, fontWeight: 400, whiteSpace: 'nowrap', ...item(s(0.55)), opacity: 0.92 * rise(s(0.55))}}>{str(props.sub)}</div>
        ) : null}
        {props.button ? (
          <div
            style={{
              position: 'absolute',
              top: 1430 * u,
              padding: `${30 * u}px ${56 * u}px`,
              borderRadius: 999,
              background: C.accent,
              color: C.ink,
              fontSize: 50 * u,
              fontWeight: 700,
              whiteSpace: 'nowrap',
              opacity: rise(s(0.85)),
              transform: `translateY(${(1 - rise(s(0.85))) * 40 * u}px) scale(${pulse})`,
            }}
          >
            {str(props.button)}
          </div>
        ) : null}
        {props.domain ? (
          <div style={{position: 'absolute', top: 1600 * u, fontSize: 54 * u, fontWeight: 500, letterSpacing: 1 * u, whiteSpace: 'nowrap', ...item(s(1.1))}}>{str(props.domain)}</div>
        ) : null}
      </div>
    </div>
  );
};

// ---- pieces of ready-made scenes

/** Before/after: a paper divider with a round knob and a chip naming each side. */
const BeforeAfterChrome: React.FC<ChromeProps> = ({d, afterSide, labels, labelInset, labelTop}) => {
  const u = useUnit();
  const beforeSide = afterSide === 'left' ? 'right' : 'left';
  return (
    <>
      <div style={{position: 'absolute', top: 0, bottom: 0, left: `${d * 100}%`, width: 6 * u, marginLeft: -3 * u, background: C.paper, boxShadow: '0 0 20px rgba(0,0,0,0.35)'}} />
      <div
        style={{
          position: 'absolute',
          top: '44.8%',
          left: `${d * 100}%`,
          width: 92 * u,
          height: 92 * u,
          marginLeft: -46 * u,
          borderRadius: 46 * u,
          background: C.paper,
          boxShadow: '0 6px 24px rgba(0,0,0,0.35)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: C.ink,
          fontFamily: FONT,
          fontWeight: 700,
          fontSize: 40 * u,
        }}
      >
        {'‹ ›'}
      </div>
      {labels ? <Chip label={labels.after} dot={C.accent} style={{[afterSide]: labelInset, top: labelTop}} /> : null}
      {labels ? <Chip label={labels.before} dot={C.negative} style={{[beforeSide]: labelInset, top: labelTop}} /> : null}
    </>
  );
};

/** The frame around media in card and picture-in-picture layouts: a thin paper edge. */
const Frame: React.FC<{children: React.ReactNode; style?: React.CSSProperties}> = ({children, style}) => {
  const u = useUnit();
  return <div style={{borderRadius: 24 * u, border: `${4 * u}px solid ${C.paper}`, boxShadow: '0 24px 60px rgba(0,0,0,0.45)', overflow: 'hidden', ...style}}>{children}</div>;
};

export const look: Look = {
  name: 'editorial',
  tokens,
  background: C.ink,
  Caption,
  Tag,
  Text,
  cards: {end: EndCard},
  cardZones: {end: 'full'},
  Frame,
  parts: {BeforeAfterChrome},
  // Calm entrances: slower and without overshoot.
  motion: {enter: {damping: 26, stiffness: 170, mass: 0.7}},
};
