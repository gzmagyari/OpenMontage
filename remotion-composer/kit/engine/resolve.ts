// Turns an edit (decisions pinned to words, lines and seconds) into exact frames.
// Pure and deterministic: the same edit, voice timings and media facts always give the same
// timeline, and everything that could not be honoured is reported, never silently patched.

import type {
  Anchor, ClipSource, Diag, Edit, EnterSpec, LayoutSpec, MainItem, MotionSpec, Resolved, ResolvedClip,
  ResolvedItem, ResolvedLayout, ResolvedMotion, ResolvedOverlay, Voice, VoiceLine, VoicePart, ZoneName,
} from './types.ts';
import {MIN_ITEM_FRAMES, TAKE_LIMIT_SECONDS} from './types.ts';
import {groupWords, takePhrases, timeGroups, type CaptionRules, type GroupSec} from './captions.ts';
import {findPhrase, occurrences, suggestions, type SpokenWord} from './words.ts';
import {parseSize, safeArea, zonesFor} from './zones.ts';

export type MediaInfo = {exists: boolean; duration?: number};

export type ResolveInput = {
  edit: Edit;
  /** Real timings from `kit voice`. Without them the script text gives an estimate. */
  voice?: Voice | null;
  /** Facts about the files the edit names. Omitted: every file is assumed present. */
  media?: Record<string, MediaInfo>;
  /** Draft: files that are still being generated are placeholders, not errors. */
  draft?: boolean;
  captionRules?: Partial<CaptionRules>;
};

const EPS = 1e-6;
// Spoken characters per second, measured on three delivered lines of one production video (range
// 12.8 to 13.6). Only used before real timings exist.
const CHARS_PER_SECOND = 13.4;

const ENTER_FRAMES: Record<string, number> = {fade: 8, wipe: 8, flash: 4};
const DEFAULT_ENTER_FRAMES = 6;

const MOTION_PRESETS: Record<string, Omit<ResolvedMotion, 'origin'>> = {
  'push-in': {scale: [1, 1.06], x: [0, 0], y: [0, 0]},
  'pull-out': {scale: [1.06, 1], x: [0, 0], y: [0, 0]},
  'pan-left': {scale: [1.08, 1.08], x: [2.5, -2.5], y: [0, 0]},
  'pan-right': {scale: [1.08, 1.08], x: [-2.5, 2.5], y: [0, 0]},
  'pan-up': {scale: [1.08, 1.08], x: [0, 0], y: [2.5, -2.5]},
  'pan-down': {scale: [1.08, 1.08], x: [0, 0], y: [-2.5, 2.5]},
  drift: {scale: [1.03, 1.07], x: [-1, 1], y: [0, 0]},
  hold: {scale: [1.06, 1.06], x: [0, 0], y: [0, 0]},
};
// What moves when the edit says nothing: every shot breathes, stills most of all.
const DEFAULT_MOTION: Record<string, ResolvedMotion | null> = {
  take: {scale: [1, 1.06], x: [0, 0], y: [0, 0], origin: '50% 40%'},
  video: {scale: [1, 1.05], x: [0, 0], y: [0, 0], origin: '50% 45%'},
  image: {scale: [1, 1.08], x: [0, 0], y: [0, 0], origin: '50% 45%'},
  freeze: {scale: [1.06, 1.06], x: [0, 0], y: [0, 0], origin: '50% 40%'},
  scene: null,
  color: null,
  layout: null,
};

export const takePath = (partId: string): string => `assets/video/take_${partId}.mp4`;

/** Timings guessed from the script, so an edit can be checked and previewed before any voice exists. */
export function estimateVoice(edit: Edit): Voice {
  const lines: VoiceLine[] = [];
  let cursor = 0;
  for (const line of edit.voice?.lines ?? []) {
    const text = String(line.text ?? '').trim();
    const tokens = text.split(/\s+/).filter(Boolean);
    const dur = Math.max(0.6, text.length / CHARS_PER_SECOND);
    const weights = tokens.map((t) => t.length + 1 + (/[.!?]$/.test(t) ? 3 : /[,;:]$/.test(t) ? 1.5 : 0));
    const total = weights.reduce((a, b) => a + b, 0) || 1;
    let t = cursor;
    const words = tokens.map((tok, i) => {
      const span = (weights[i] / total) * dur;
      const w = {text: tok, start: round3(t), end: round3(t + span * (tok.length + 1) / weights[i])};
      t += span;
      return w;
    });
    const take = line.take === true ? takePath(line.id) : typeof line.take === 'string' ? line.take : undefined;
    lines.push({id: line.id, text, parts: [{id: line.id, start: round3(cursor), dur: round3(dur), take, words}]});
    cursor += dur + (edit.voice?.gap ?? 0);
  }
  return {lines, estimated: true};
}

const round3 = (n: number): number => Math.round(n * 1000) / 1000;

export function resolveEdit(input: ResolveInput): Resolved {
  const {edit, media, draft = false} = input;
  const diagnostics: Diag[] = [];
  const diag = (level: Diag['level'], code: string, message: string, where?: string) =>
    diagnostics.push({level, code, message, where});

  const fps = edit.canvas?.fps ?? 30;
  const size = parseSize(edit.canvas?.size) ?? {width: 1080, height: 1920};
  const fr = (s: number) => Math.round(s * fps);

  // ---- voice: the timing spine
  const scripted = (edit.voice?.lines ?? []).length > 0;
  let voice: Voice = {lines: []};
  if (input.voice?.lines?.length) {
    voice = input.voice;
    for (const line of edit.voice?.lines ?? []) {
      const timed = voice.lines.find((l) => l.id === line.id);
      if (!timed) diag('error', 'voice-stale', `Line "${line.id}" has no timings yet. Run \`kit voice\` again after changing the script.`, 'voice');
      else if (timed.text !== undefined && timed.text.trim() !== String(line.text).trim()) {
        diag('error', 'voice-stale', `The text of line "${line.id}" changed after its voice was timed. Regenerate that line and run \`kit voice\` again.`, 'voice');
      }
    }
  } else if (scripted) {
    voice = estimateVoice(edit);
    diag('info', 'voice-estimated', 'No voice timings yet: cuts are placed from an estimate of the script. They become exact after `kit voice`.');
  }
  const estimated = voice.estimated === true;
  const partsOf = (lineId: string): VoicePart[] => voice.lines.find((l) => l.id === lineId)?.parts ?? [];
  const allParts: Array<VoicePart & {line: string}> = voice.lines.flatMap((l) => l.parts.map((p) => ({...p, line: l.id})));
  const words: SpokenWord[] = allParts.flatMap((p) => p.words.map((w) => ({...w, line: p.line, part: p.id})));
  const voiceEnd = allParts.reduce((m, p) => Math.max(m, p.start + p.dur), 0);
  const voiceStart = allParts.length ? Math.min(...allParts.map((p) => p.start)) : 0;

  for (const line of edit.voice?.lines ?? []) {
    if (!line.take) continue;
    for (const part of partsOf(line.id)) {
      if (part.dur <= TAKE_LIMIT_SECONDS + 0.01) continue;
      if (estimated) {
        diag('warning', 'line-long-for-take', `Line "${line.id}" will run about ${part.dur.toFixed(1)} s; one presenter take holds ${TAKE_LIMIT_SECONDS} s. \`kit voice\` will split it at a sentence pause into two takes, or shorten the line.`, 'voice');
      } else {
        diag('error', 'take-over-limit', `Part "${part.id}" of line "${line.id}" is ${part.dur.toFixed(1)} s, over the ${TAKE_LIMIT_SECONDS} s a take holds. Run \`kit voice\` (it splits long lines) or shorten the line.`, 'voice');
      }
    }
  }

  // ---- files
  const pending = new Set<string>();
  const missing = (path: string | undefined, where: string): boolean => {
    if (!path || !media) return false;
    if (media[path]?.exists) return false;
    if (!pending.has(path) && !draft) diag('error', 'asset-missing', `${path} does not exist.`, where);
    pending.add(path);
    return true;
  };

  // ---- moments
  const itemSpans = new Map<string, {from: number; to: number}>();
  let total = 0;
  const at = (anchor: Anchor, where: string, ctx: {after: number; allowEnd: boolean}): number | null => {
    if (typeof anchor === 'number') return anchor;
    const offset = ('offset' in anchor && anchor.offset) || 0;
    if ('t' in anchor) return anchor.t + offset;
    if ('word' in anchor) {
      const hit = findPhrase(words, anchor.word, {after: ctx.after, line: anchor.line, nth: anchor.nth});
      if (!hit) {
        const anywhere = occurrences(words, anchor.word, anchor.line);
        const hint = anywhere.length
          ? `It is spoken at ${anywhere.map((m) => m[0].start.toFixed(1) + ' s').join(', ')}, which is before this point (${ctx.after.toFixed(1)} s).`
          : `The script does not contain it${anchor.line ? ` in line "${anchor.line}"` : ''}. Close words: ${suggestions(words, anchor.word).join(', ') || 'none'}.`;
        diag('error', 'anchor-word-not-found', `"${anchor.word}" was not found. ${hint}`, where);
        return null;
      }
      return (anchor.edge === 'end' ? hit[hit.length - 1].end : hit[0].start) + offset;
    }
    if ('line' in anchor) {
      const parts = partsOf(anchor.line);
      if (!parts.length) {
        diag('error', 'anchor-line-unknown', `Line "${anchor.line}" has no timings.`, where);
        return null;
      }
      const last = parts[parts.length - 1];
      return (anchor.edge === 'end' ? last.start + last.dur : parts[0].start) + offset;
    }
    if ('voice' in anchor) {
      if (!allParts.length) {
        diag('error', 'anchor-no-voice', 'This moment refers to the voice, but the edit has no voice lines.', where);
        return null;
      }
      return (anchor.voice === 'end' ? voiceEnd : voiceStart) + offset;
    }
    if ('item' in anchor) {
      const span = itemSpans.get(anchor.item);
      if (!span) {
        diag('error', 'anchor-item-unknown', `No main item with id "${anchor.item}" comes before this point.`, where);
        return null;
      }
      return (anchor.edge === 'end' ? span.to : span.from) + offset;
    }
    if ('end' in anchor) {
      if (!ctx.allowEnd) {
        diag('error', 'anchor-invalid', '{"end": true} is the end of the video; use it on overlays, not on main items.', where);
        return null;
      }
      return total + offset;
    }
    return null;
  };

  // ---- clips
  const motionFor = (kind: string, spec: MotionSpec | undefined): ResolvedMotion | null => {
    const base = DEFAULT_MOTION[kind] ?? null;
    if (spec === undefined) return base;
    const type = typeof spec === 'string' ? spec : spec.type ?? 'push-in';
    if (type === 'none') return null;
    const preset = MOTION_PRESETS[type] ?? MOTION_PRESETS['push-in'];
    const origin = (typeof spec === 'object' && spec.origin) || base?.origin || '50% 45%';
    const out: ResolvedMotion = {scale: [...preset.scale], x: [...preset.x], y: [...preset.y], origin};
    if (typeof spec === 'object') {
      if (typeof spec.from === 'number') out.scale[0] = spec.from;
      if (typeof spec.to === 'number') out.scale[1] = spec.to;
      if (type === 'hold' && typeof spec.to === 'number' && typeof spec.from !== 'number') out.scale[0] = spec.to;
    }
    return out;
  };

  const partAt = (lineId: string, t: number): {part: VoicePart; inside: boolean; before: boolean} | null => {
    const parts = partsOf(lineId);
    if (!parts.length) return null;
    for (const part of parts) {
      if (t >= part.start - EPS && t < part.start + part.dur - EPS) return {part, inside: true, before: false};
    }
    if (t < parts[0].start) return {part: parts[0], inside: false, before: true};
    let prev = parts[0];
    for (const part of parts) if (part.start <= t + EPS) prev = part;
    return {part: prev, inside: false, before: false};
  };

  const clipFor = (src: ClipSource, from: number, to: number, where: string): ResolvedClip => {
    const base = {fit: src.fit ?? 'cover', speed: src.speed ?? 1, volume: src.audio ?? 0, props: src.props};
    if (src.take !== undefined) {
      const found = partAt(src.take, from);
      if (!found) {
        diag('error', 'take-unknown-line', `"${src.take}" is not a voice line with a take.`, where);
        return {kind: 'color', color: '#000000', motion: null, ...base};
      }
      const path = found.part.take ?? takePath(found.part.id);
      const gone = missing(path, where);
      if (found.inside) {
        return {kind: 'take', src: path, trimBefore: Math.max(0, fr(from) - fr(found.part.start)), motion: motionFor('take', src.motion), missing: gone, ...base};
      }
      // A few frames between two lines are a normal hold, not worth a finding.
      if (to - from > 0.2) {
        diag('warning', 'take-outside-voice', `The take of line "${src.take}" is on screen from ${from.toFixed(2)} s to ${to.toFixed(2)} s, where that line is not being spoken; its ${found.before ? 'first' : 'last'} frame is held.`, where);
      }
      return {kind: 'freeze', src: path, freezeFrame: found.before ? 0 : Math.max(0, fr(found.part.dur) - 3), motion: motionFor('freeze', src.motion), missing: gone, ...base};
    }
    if (src.freeze !== undefined) {
      const parts = partsOf(src.freeze);
      if (parts.length) {
        const last = parts[parts.length - 1];
        const path = last.take ?? takePath(last.id);
        return {kind: 'freeze', src: path, freezeFrame: Math.max(0, fr(last.dur) - 3), motion: motionFor('freeze', src.motion), missing: missing(path, where), ...base};
      }
      const gone = missing(src.freeze, where);
      const duration = media?.[src.freeze]?.duration;
      if (/\.(png|jpe?g|webp|gif)$/i.test(src.freeze)) return {kind: 'image', src: src.freeze, motion: motionFor('freeze', src.motion), missing: gone, ...base};
      return {kind: 'freeze', src: src.freeze, freezeFrame: duration ? Math.max(0, fr(duration) - 3) : 0, motion: motionFor('freeze', src.motion), missing: gone, ...base};
    }
    if (src.video !== undefined) {
      const gone = missing(src.video, where);
      const duration = media?.[src.video]?.duration;
      const need = (src.start ?? 0) + (to - from) * (src.speed ?? 1);
      if (duration !== undefined && need > duration + 0.08) {
        diag('warning', 'video-shorter-than-shot', `${src.video} is ${duration.toFixed(1)} s but this shot needs ${need.toFixed(1)} s of it; it will hold on its last frame.`, where);
      }
      return {kind: 'video', src: src.video, trimBefore: fr(src.start ?? 0), motion: motionFor('video', src.motion), missing: gone, ...base};
    }
    if (src.image !== undefined) return {kind: 'image', src: src.image, motion: motionFor('image', src.motion), missing: missing(src.image, where), ...base};
    if (src.scene !== undefined) return {kind: 'scene', scene: src.scene, motion: motionFor('scene', src.motion), ...base, props: src.props ?? {}};
    return {kind: 'color', color: src.color ?? '#000000', motion: motionFor('color', src.motion), ...base};
  };

  const layoutFor = (layout: LayoutSpec, from: number, to: number, where: string): ResolvedLayout => {
    const slot = (s: ClipSource, name: string) => clipFor(s, from, to, `${where}.${name}`);
    switch (layout.type) {
      case 'split':
        return {type: 'split', ratio: layout.ratio ?? 0.5, direction: layout.direction ?? 'vertical', slots: layout.slots.map((s, i) => slot(s, `slots[${i}]`))};
      case 'grid':
        return {type: 'grid', columns: layout.columns ?? 2, slots: layout.slots.map((s, i) => slot(s, `slots[${i}]`))};
      case 'pip':
        return {type: 'pip', base: slot(layout.base, 'base'), inset: slot(layout.inset, 'inset'), corner: layout.corner ?? 'bottom-left', size: layout.size ?? 0.34};
      case 'card':
        return {type: 'card', media: slot(layout.media, 'media'), aspect: layout.aspect ?? 3 / 2, width: layout.width ?? 0.92, top: layout.top ?? null, backdrop: layout.backdrop ? slot(layout.backdrop, 'backdrop') : null};
    }
  };

  /** Lines whose takes this item shows: their part boundaries are where its footage changes. */
  const takeLines = (item: MainItem): string[] => {
    const out: string[] = [];
    const visit = (s: ClipSource | undefined) => s?.take !== undefined && out.push(s.take);
    visit(item);
    const l = item.layout;
    if (l) {
      if (l.type === 'split' || l.type === 'grid') l.slots.forEach(visit);
      if (l.type === 'pip') [l.base, l.inset].forEach(visit);
      if (l.type === 'card') [l.media, l.backdrop].forEach(visit);
    }
    return out;
  };

  const enterFor = (spec: EnterSpec | undefined): {type: string; frames: number} => {
    const type = spec === undefined ? 'none' : typeof spec === 'string' ? spec : spec.type;
    if (type === 'none' || type === 'cut') return {type: 'none', frames: 0};
    const frames = (typeof spec === 'object' && spec.frames) || ENTER_FRAMES[type] || DEFAULT_ENTER_FRAMES;
    return {type, frames};
  };

  const labelOf = (item: MainItem): string =>
    item.take !== undefined ? `take ${item.take}`
    : item.video !== undefined ? item.video.split('/').pop()!
    : item.image !== undefined ? item.image.split('/').pop()!
    : item.scene !== undefined ? `scene ${item.scene}`
    : item.freeze !== undefined ? `freeze ${item.freeze.split('/').pop()}`
    : item.layout !== undefined ? `layout ${item.layout.type}`
    : `color ${item.color ?? ''}`;

  // ---- the main sequence: each item starts where the previous one ended
  const main: ResolvedItem[] = [];
  let cursor = 0;
  const list = Array.isArray(edit.main) ? edit.main : [];
  list.forEach((item, index) => {
    const where = `main[${index}]`;
    const id = item.id ?? `m${index + 1}`;
    const from = cursor;
    let to: number | null;
    if (typeof item.for === 'number') to = from + item.for;
    else if (item.until !== undefined) to = at(item.until, `${where}.until`, {after: from, allowEnd: false});
    else if (item.take !== undefined && partsOf(item.take).length) {
      const parts = partsOf(item.take);
      to = parts[parts.length - 1].start + parts[parts.length - 1].dur;
    } else if (index === list.length - 1 && voiceEnd > from + EPS) to = voiceEnd;
    else {
      diag('error', 'main-until-missing', `This item has no end. Give it "until" (a word, a line, a time) or "for" (seconds).`, where);
      to = null;
    }
    if (to === null) return;
    if (to < from - EPS) {
      diag('warning', 'until-before-start', `This item would end at ${to.toFixed(2)} s, before it starts (${from.toFixed(2)} s). It is skipped.`, where);
      return;
    }
    if (fr(to) - fr(from) < MIN_ITEM_FRAMES) {
      diag('info', 'item-dropped-short', `This item would be on screen for under ${MIN_ITEM_FRAMES} frames (${from.toFixed(2)} to ${to.toFixed(2)} s) and is skipped.`, where);
      return;
    }

    const cuts = new Set<number>();
    for (const lineId of takeLines(item)) {
      for (const p of partsOf(lineId)) {
        for (const edge of [p.start, p.start + p.dur]) if (edge > from + EPS && edge < to - EPS && fr(edge) > fr(from) && fr(edge) < fr(to)) cuts.add(edge);
      }
    }
    const bounds = [from, ...[...cuts].sort((a, b) => a - b), to];
    for (let k = 0; k + 1 < bounds.length; k++) {
      const a = bounds[k];
      const b = bounds[k + 1];
      if (fr(b) - fr(a) < 1) continue;
      const clip: ResolvedClip = item.layout
        ? {kind: 'layout', layout: layoutFor(item.layout, a, b, `${where}.layout`), motion: null, fit: 'cover', speed: 1, volume: 0}
        : clipFor(item, a, b, where);
      main.push({...clip, id: k === 0 ? id : `${id}.${k + 1}`, from: fr(a), to: fr(b), tail: 0, enter: k === 0 ? enterFor(item.enter) : {type: 'none', frames: 0}, label: labelOf(item)});
    }
    itemSpans.set(id, {from, to});
    cursor = to;
  });
  total = cursor;
  main.forEach((seg, i) => {
    const next = main[i + 1];
    seg.tail = next && next.enter.frames > 0 ? next.enter.frames + 2 : 0;
  });
  if (main.length && main[0].enter.frames > 0) main[0].enter = {type: 'none', frames: 0};

  // ---- overlays
  const overlays: ResolvedOverlay[] = [];
  (edit.overlays ?? []).forEach((ov, index) => {
    const where = `overlays[${index}]`;
    const kind = (['tag', 'card', 'text', 'scene'] as const).find((k) => ov[k] !== undefined);
    if (!kind) return;
    const name = String(ov[kind]);
    let from: number | null = 0;
    if (ov.from !== undefined) {
      from = at(ov.from, `${where}.from`, {after: -Infinity, allowEnd: true});
      if (typeof ov.from === 'object' && 'word' in ov.from && ov.from.line === undefined && ov.from.nth === undefined) {
        const n = occurrences(words, ov.from.word).length;
        if (n > 1) diag('warning', 'anchor-ambiguous', `"${ov.from.word}" is spoken ${n} times; the first one is used. Add "line" or "nth" to pick another.`, `${where}.from`);
      }
    }
    if (from === null) return;
    let to: number | null = total;
    if (typeof ov.for === 'number') to = from + ov.for;
    else if (ov.until !== undefined) to = at(ov.until, `${where}.until`, {after: from, allowEnd: true});
    if (to === null) return;
    const a = Math.max(0, Math.min(from, total));
    const b = Math.max(0, Math.min(to, total));
    if (fr(b) - fr(a) < MIN_ITEM_FRAMES) {
      diag('warning', 'overlay-dropped', `This overlay would be on screen for under ${MIN_ITEM_FRAMES} frames (${from.toFixed(2)} to ${to.toFixed(2)} s) and is skipped.`, where);
      return;
    }
    overlays.push({
      id: ov.id ?? `o${index + 1}`,
      kind,
      name: kind === 'tag' || kind === 'text' ? kind : name,
      text: kind === 'tag' || kind === 'text' ? name : undefined,
      props: ov.props ?? {},
      from: fr(a),
      to: fr(b),
      zone: (ov.zone as ZoneName | undefined) ?? null,
    });
  });

  // ---- captions
  const wantCaptions = edit.captions?.enabled !== false && words.length > 0;
  const rules: Partial<CaptionRules> = {...input.captionRules};
  if (edit.captions?.maxWords) rules.maxWords = edit.captions.maxWords;
  if (edit.captions?.maxChars) rules.maxChars = edit.captions.maxChars;
  let groups: GroupSec[] = [];
  if (wantCaptions) {
    const timed = words.map((w) => ({text: w.text, start: w.start, end: w.end, part: w.part}));
    const byHand = takePhrases(timed, edit.captions?.phrases ?? []);
    if (byHand.failed !== null) {
      const next = timed.slice(byHand.used, byHand.used + 6).map((w) => w.text).join(' ');
      diag('warning', 'caption-phrase-mismatch', `Caption phrase "${byHand.failed}" does not match what is spoken next ("${next}"). From there on the captions are grouped automatically.`, 'captions.phrases');
    }
    groups = [...timeGroups(byHand.groups, rules, voiceEnd), ...groupWords(timed.slice(byHand.used), rules, voiceEnd)];
    // The hand-set part ends where the automatic part begins.
    for (let i = 0; i + 1 < groups.length; i++) groups[i].visibleEnd = Math.min(groups[i].visibleEnd, Math.max(groups[i].end, groups[i + 1].start));
  }
  const captionGroups = groups.map((g, i) => {
    const nextFrom = groups[i + 1] ? fr(groups[i + 1].start) : Infinity;
    return {
      from: fr(g.start),
      to: Math.max(fr(g.start) + 1, Math.min(fr(g.visibleEnd), nextFrom)),
      words: g.words.map((w) => ({text: w.text, from: fr(w.start), to: fr(w.end)})),
      spaced: g.spaced,
      rtl: g.rtl,
    };
  });

  const mix = edit.audio?.mix ?? null;
  if (mix) missing(mix, 'audio.mix');

  return {
    version: 1,
    fps,
    width: size.width,
    height: size.height,
    durationInFrames: Math.max(1, fr(total)),
    voiceEndFrame: fr(voiceEnd),
    safe: safeArea(size.width, size.height, edit.canvas?.safe),
    zones: zonesFor(size.width, size.height, edit.canvas?.safe),
    main,
    overlays,
    captions: {enabled: wantCaptions, variant: edit.captions?.variant ?? null, zone: edit.captions?.zone ?? 'lower', groups: captionGroups},
    audio: {mix},
    pending: [...pending].sort(),
    voiceEstimated: estimated,
    diagnostics,
  };
}
