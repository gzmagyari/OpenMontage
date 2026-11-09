// The whole video: the main sequence, overlays, the look's finish, captions and the soundtrack.

import React from 'react';
import {AbsoluteFill, Audio, Sequence, continueRender, delayRender, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {EXIT_FRAMES, KitContext, ShotContext, useKit, type Look, type SceneRegistry} from './context.tsx';
import {ClipView, Enter, Placeholder} from './stage.tsx';
import type {Rect, Resolved, ResolvedOverlay, ZoneName} from './types.ts';

const zoneOf = (o: ResolvedOverlay, look: Look): ZoneName =>
  o.zone ?? (o.kind === 'card' ? look.cardZones?.[o.name] : undefined) ?? (o.kind === 'text' ? 'center' : 'upper');

const OverlayView: React.FC<{overlay: ResolvedOverlay}> = ({overlay}) => {
  const {look, resolved, scenes} = useKit();
  const zone = resolved.zones[zoneOf(overlay, look)];
  const frames = overlay.to - overlay.from;
  let body: React.ReactNode;
  if (overlay.kind === 'tag') body = <look.Tag text={overlay.text ?? ''} props={overlay.props} zone={zone} frames={frames} />;
  else if (overlay.kind === 'text') {
    const Text = look.Text ?? look.Tag;
    body = <Text text={overlay.text ?? ''} props={overlay.props} zone={zone} frames={frames} />;
  } else if (overlay.kind === 'card') {
    const Card = look.cards[overlay.name];
    if (Card) body = <Card props={overlay.props} zone={zone} frames={frames} />;
    else {
      console.log(`KITDIAG ${JSON.stringify({error: 'card-unknown', card: overlay.name, known: Object.keys(look.cards)})}`);
      body = <Placeholder label={`card "${overlay.name}" (this look has: ${Object.keys(look.cards).join(', ')})`} />;
    }
  } else {
    const Scene = scenes[overlay.name];
    if (Scene) body = <Scene {...overlay.props} zone={zone} frames={frames} />;
    else {
      console.log(`KITDIAG ${JSON.stringify({error: 'scene-unknown', scene: overlay.name, known: Object.keys(scenes)})}`);
      body = null;
    }
  }
  return (
    <ShotContext.Provider value={{id: overlay.id, frames, from: overlay.from}}>
      <div data-kit-overlay={overlay.id} data-kit-zone={zoneOf(overlay, look)} style={{position: 'absolute', inset: 0}}>
        {body}
      </div>
    </ShotContext.Provider>
  );
};

const CaptionLayer: React.FC = () => {
  const frame = useCurrentFrame();
  const {resolved, look} = useKit();
  const {captions} = resolved;
  if (!captions.enabled || frame >= resolved.voiceEndFrame) return null;
  const group = captions.groups.find((g) => frame >= g.from && frame < g.to);
  if (!group) return null;
  let active = 0;
  group.words.forEach((w, i) => {
    if (frame >= w.from) active = i;
  });
  return (
    <div data-kit-captions="1" style={{position: 'absolute', inset: 0}}>
      <look.Caption group={group} active={active} localFrame={frame - group.from} frame={frame} zone={resolved.zones[captions.zone]} variant={captions.variant} />
    </div>
  );
};

/** Check sheets only: which frame this is and what is on screen. */
const DebugLabel: React.FC = () => {
  const frame = useCurrentFrame();
  const {resolved} = useKit();
  const item = resolved.main.find((m) => frame >= m.from && frame < m.to);
  const text = `${(frame / resolved.fps).toFixed(2)}s  f${frame}  ${item ? `${item.id} ${item.label}` : ''}`;
  return (
    <div style={{position: 'absolute', left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.78)', color: '#fff', fontFamily: 'DejaVu Sans Mono, monospace', fontSize: resolved.width * 0.03, padding: '14px 20px', whiteSpace: 'nowrap', overflow: 'hidden'}}>
      {text}
    </div>
  );
};

/** True when the element itself draws something: text, a picture, a fill, a border or a shadow. */
const draws = (node: Element): boolean => {
  if (/^(IMG|VIDEO|CANVAS|SVG)$/i.test(node.tagName)) return true;
  for (const child of node.childNodes) if (child.nodeType === 3 && (child.textContent ?? '').trim()) return true;
  const style = getComputedStyle(node);
  const clear = (c: string) => c === 'transparent' || /rgba\(.*,\s*0\)$/.test(c);
  return !clear(style.backgroundColor) || style.backgroundImage !== 'none' || parseFloat(style.borderTopWidth) > 0 || style.boxShadow !== 'none';
};

/** The box that holds everything visible inside `el`, in canvas pixels. */
const rectOf = (el: Element, origin: DOMRect, scale: number, canvasW: number, canvasH: number): Rect | null => {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  el.querySelectorAll('*').forEach((node) => {
    const r = node.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || !draws(node)) return;
    // Full-canvas dim layers say nothing about where the content sits.
    if (r.width >= canvasW * scale * 0.98 && r.height >= canvasH * scale * 0.98) return;
    if (parseFloat(getComputedStyle(node).opacity) < 0.05) return;
    x0 = Math.min(x0, r.left);
    y0 = Math.min(y0, r.top);
    x1 = Math.max(x1, r.right);
    y1 = Math.max(y1, r.bottom);
  });
  if (!Number.isFinite(x0)) return null;
  const px = (v: number) => Math.round(v / scale);
  return {x: px(x0 - origin.left), y: px(y0 - origin.top), w: px(x1 - x0), h: px(y1 - y0)};
};

/** Check sheets only: reports where overlays and captions really landed, for the layout checks.
 *  It holds the frame until fonts are in and layout has settled, then measures. */
const Probe: React.FC = () => {
  const frame = useCurrentFrame();
  const {width, height} = useVideoConfig();
  const ref = React.useRef<HTMLDivElement>(null);
  React.useLayoutEffect(() => {
    const handle = delayRender(`kit layout probe, frame ${frame}`);
    let done = false;
    const finish = (measure: boolean) => {
      if (done) return;
      done = true;
      const root = ref.current?.parentElement;
      if (measure && root) {
        const origin = root.getBoundingClientRect();
        const scale = origin.width / width || 1;
        const overlays = [...root.querySelectorAll('[data-kit-overlay]')].map((el) => ({
          id: el.getAttribute('data-kit-overlay'),
          zone: el.getAttribute('data-kit-zone'),
          rect: rectOf(el, origin, scale, width, height),
        }));
        const cap = root.querySelector('[data-kit-captions]');
        console.log(`KITDIAG ${JSON.stringify({frame, overlays, captions: cap ? rectOf(cap, origin, scale, width, height) : null})}`);
      }
      continueRender(handle);
    };
    const settled = () => requestAnimationFrame(() => requestAnimationFrame(() => finish(true)));
    (document.fonts?.ready ?? Promise.resolve()).then(settled, settled);
    return () => finish(false);
  }, [frame, width, height]);
  return <div ref={ref} style={{display: 'none'}} />;
};

export const KitVideo: React.FC<{resolved: Resolved; look: Look; scenes: SceneRegistry}> = ({resolved, look, scenes}) => {
  const value = React.useMemo(
    () => ({resolved, look, scenes, canvas: {width: resolved.width, height: resolved.height, fps: resolved.fps}}),
    [resolved, look, scenes],
  );
  const Finish = look.Finish;
  return (
    <KitContext.Provider value={value}>
      <AbsoluteFill style={{background: look.background ?? '#000'}}>
        {resolved.audio.mix && !resolved.pending.includes(resolved.audio.mix) ? <Audio src={staticFile(resolved.audio.mix)} /> : null}
        {resolved.main.map((item) => {
          const frames = item.to - item.from;
          return (
            <Sequence key={item.id} from={item.from} durationInFrames={frames + item.tail} name={item.id}>
              <ShotContext.Provider value={{id: item.id, frames, from: item.from}}>
                <Enter enter={item.enter}>
                  <ClipView clip={item} frames={frames} />
                </Enter>
              </ShotContext.Provider>
            </Sequence>
          );
        })}
        {resolved.overlays.map((o) => (
          <Sequence key={o.id} from={o.from} durationInFrames={o.to - o.from + EXIT_FRAMES} name={o.id}>
            <OverlayView overlay={o} />
          </Sequence>
        ))}
        {Finish ? <Finish /> : null}
        <CaptionLayer />
        {resolved.debug ? <DebugLabel /> : null}
        {resolved.debug ? <Probe /> : null}
      </AbsoluteFill>
    </KitContext.Provider>
  );
};
