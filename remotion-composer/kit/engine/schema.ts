// Checks the shape of an edit before anything is resolved or rendered. Every finding names the
// place in the file and says what is accepted there.

import type {Diag} from './types.ts';
import {SAFE_PROFILES, parseSize} from './zones.ts';

export const ENTER_TYPES = ['none', 'cut', 'fade', 'slide-left', 'slide-right', 'slide-up', 'slide-down', 'whip', 'punch', 'wipe', 'flash'];
export const MOTION_TYPES = ['none', 'push-in', 'pull-out', 'pan-left', 'pan-right', 'pan-up', 'pan-down', 'drift', 'hold'];
const ZONES = ['upper', 'center', 'lower', 'full'];
const FITS = ['cover', 'contain', 'blur'];
const SOURCES = ['take', 'video', 'image', 'scene', 'freeze', 'color'];
const CLIP_KEYS = [...SOURCES, 'props', 'motion', 'fit', 'start', 'speed', 'audio'];
const MAIN_KEYS = [...CLIP_KEYS, 'id', 'layout', 'until', 'for', 'enter'];
const OVERLAY_KINDS = ['tag', 'card', 'text', 'scene'];
const OVERLAY_KEYS = [...OVERLAY_KINDS, 'id', 'props', 'from', 'until', 'for', 'zone'];
const ROOT_KEYS = ['canvas', 'target_seconds', 'voice', 'music', 'audio', 'captions', 'main', 'overlays'];
const ANCHOR_KINDS = ['t', 'word', 'line', 'voice', 'item', 'end'];

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v);
const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const isStr = (v: unknown): v is string => typeof v === 'string' && v.trim().length > 0;

export function validateEdit(edit: unknown): Diag[] {
  const out: Diag[] = [];
  const err = (where: string, message: string, code = 'edit-invalid') => out.push({level: 'error', code, message, where});
  const warn = (where: string, message: string, code = 'edit-unknown-key') => out.push({level: 'warning', code, message, where});

  const unknownKeys = (obj: Obj, allowed: string[], where: string) => {
    for (const key of Object.keys(obj)) {
      if (allowed.includes(key)) continue;
      const near = allowed.find((a) => a.startsWith(key.slice(0, 3)) || key.startsWith(a.slice(0, 3)));
      warn(`${where}.${key}`, `"${key}" is not a known key here and is ignored${near ? ` (did you mean "${near}"?)` : ''}. Known: ${allowed.join(', ')}.`);
    }
  };

  if (!isObj(edit)) {
    err('edit', 'The edit must be a JSON object with at least a "main" list.');
    return out;
  }
  unknownKeys(edit, ROOT_KEYS, 'edit');

  const lineIds = new Set<string>();
  if (edit.canvas !== undefined) {
    if (!isObj(edit.canvas)) err('canvas', 'canvas must be an object like {"size": "1080x1920", "fps": 30, "safe": "tiktok"}.');
    else {
      const c = edit.canvas;
      if (c.size !== undefined && (typeof c.size !== 'string' || !parseSize(c.size))) err('canvas.size', 'size must look like "1080x1920".');
      if (c.fps !== undefined && (!isNum(c.fps) || c.fps < 12 || c.fps > 60)) err('canvas.fps', 'fps must be a number from 12 to 60.');
      if (c.safe !== undefined && !SAFE_PROFILES.includes(c.safe as string)) err('canvas.safe', `safe must be one of: ${SAFE_PROFILES.join(', ')}.`);
    }
  }
  if (edit.target_seconds !== undefined && (!isNum(edit.target_seconds) || edit.target_seconds <= 0)) {
    err('target_seconds', 'target_seconds must be a positive number.');
  }

  if (edit.voice !== undefined) {
    const lines = isObj(edit.voice) ? edit.voice.lines : undefined;
    if (!Array.isArray(lines)) err('voice.lines', 'voice.lines must be a list of {"id", "text"} spoken lines, in speaking order.');
    else {
      lines.forEach((line, i) => {
        const where = `voice.lines[${i}]`;
        if (!isObj(line)) return err(where, 'Each line is an object like {"id": "A", "text": "...", "take": true}.');
        if (!isStr(line.id) || !/^[A-Za-z0-9_-]+$/.test(line.id)) err(`${where}.id`, 'id must be a short name of letters, digits, "_" or "-".');
        else if (lineIds.has(line.id)) err(`${where}.id`, `Line id "${line.id}" is used twice.`);
        else lineIds.add(line.id);
        if (!isStr(line.text)) err(`${where}.text`, 'text must hold the exact words spoken in this line.');
        if (line.audio !== undefined && !isStr(line.audio)) err(`${where}.audio`, 'audio is the path of the recording of this line (default: assets/audio/<id>.mp3).');
        if (line.take !== undefined && typeof line.take !== 'boolean' && !isStr(line.take)) {
          err(`${where}.take`, 'take is true (a presenter take is filmed for this line), false, or the path of the take.');
        }
      });
    }
  }

  const anchor = (value: unknown, where: string) => {
    if (isNum(value)) {
      if (value < 0) err(where, 'A time in seconds cannot be negative.');
      return;
    }
    if (!isObj(value)) return err(where, 'A moment is a number of seconds or one of {"word"}, {"line"}, {"voice"}, {"item"}, {"t"}, {"end": true}.', 'anchor-invalid');
    // Beside "word", "line" is not a second moment: it says in which line the word is meant.
    const kinds = ANCHOR_KINDS.filter((k) => value[k] !== undefined && !(k === 'line' && value.word !== undefined));
    if (kinds.length !== 1) return err(where, `A moment names exactly one of: ${ANCHOR_KINDS.join(', ')} ("line" may also go with "word", to say which line's word is meant).`, 'anchor-invalid');
    const kind = kinds[0];
    if (kind === 't' && (!isNum(value.t) || value.t < 0)) err(where, '"t" must be a time in seconds.', 'anchor-invalid');
    if (kind === 'word' && !isStr(value.word)) err(where, '"word" must be a word or short phrase from the script.', 'anchor-invalid');
    if (kind === 'line' && (!isStr(value.line) || !lineIds.has(value.line))) {
      err(where, `"line" must be one of the voice line ids${lineIds.size ? ` (${[...lineIds].join(', ')})` : ' (the edit has no voice lines)'}.`, 'anchor-invalid');
    }
    if (kind === 'word' && value.line !== undefined && !lineIds.has(value.line as string)) err(where, `"line" must be one of the voice line ids (${[...lineIds].join(', ')}).`, 'anchor-invalid');
    if (kind === 'voice' && value.voice !== 'start' && value.voice !== 'end') err(where, '"voice" is "start" or "end".', 'anchor-invalid');
    if (kind === 'item' && !isStr(value.item)) err(where, '"item" must be the id of a main item.', 'anchor-invalid');
    if (value.nth !== undefined && (!Number.isInteger(value.nth) || (value.nth as number) < 1)) err(where, '"nth" counts occurrences from 1.', 'anchor-invalid');
    if (value.edge !== undefined && value.edge !== 'start' && value.edge !== 'end') err(where, '"edge" is "start" or "end".', 'anchor-invalid');
    if (value.offset !== undefined && !isNum(value.offset)) err(where, '"offset" is a number of seconds.', 'anchor-invalid');
  };

  const clip = (c: Obj, where: string, extraSources: string[] = []) => {
    const sources = [...SOURCES, ...extraSources].filter((k) => c[k] !== undefined);
    if (sources.length !== 1) err(where, `Name exactly one thing to show: ${[...SOURCES, ...extraSources].join(', ')}.`);
    for (const key of ['take', 'video', 'image', 'scene', 'freeze', 'color']) {
      if (c[key] !== undefined && !isStr(c[key])) err(`${where}.${key}`, `${key} must be a non-empty string.`);
    }
    if (isStr(c.take) && !lineIds.has(c.take)) err(`${where}.take`, `take must be the id of a voice line (${[...lineIds].join(', ') || 'none defined'}).`);
    if (c.props !== undefined && !isObj(c.props)) err(`${where}.props`, 'props must be an object.');
    if (c.fit !== undefined && !FITS.includes(c.fit as string)) err(`${where}.fit`, `fit is one of: ${FITS.join(', ')}.`);
    if (c.start !== undefined && (!isNum(c.start) || c.start < 0)) err(`${where}.start`, 'start is a time in seconds into the clip.');
    if (c.speed !== undefined && (!isNum(c.speed) || c.speed <= 0 || c.speed > 4)) err(`${where}.speed`, 'speed is a number above 0, at most 4.');
    if (c.audio !== undefined && (!isNum(c.audio) || c.audio < 0 || c.audio > 1)) err(`${where}.audio`, 'audio is a volume from 0 to 1.');
    if (c.motion !== undefined) {
      const m = c.motion;
      const type = typeof m === 'string' ? m : isObj(m) ? (m.type ?? 'push-in') : null;
      if (type === null || !MOTION_TYPES.includes(type as string)) err(`${where}.motion`, `motion is one of: ${MOTION_TYPES.join(', ')} (or an object with type, from, to, origin).`);
    }
  };

  const layout = (l: unknown, where: string) => {
    if (!isObj(l) || !isStr(l.type)) return err(where, 'layout needs a type: split, pip, card or grid.');
    const slot = (s: unknown, w: string) => (isObj(s) ? (unknownKeys(s, CLIP_KEYS, w), clip(s, w)) : err(w, 'A layout slot is an object naming what to show.'));
    if (l.type === 'split' || l.type === 'grid') {
      if (!Array.isArray(l.slots) || l.slots.length < 2) return err(`${where}.slots`, `${l.type} needs a list of at least two slots.`);
      l.slots.forEach((s, i) => slot(s, `${where}.slots[${i}]`));
    } else if (l.type === 'pip') {
      slot(l.base, `${where}.base`);
      slot(l.inset, `${where}.inset`);
    } else if (l.type === 'card') {
      slot(l.media, `${where}.media`);
      if (l.backdrop !== undefined) slot(l.backdrop, `${where}.backdrop`);
    } else err(`${where}.type`, 'layout type is split, pip, card or grid.');
  };

  if (!Array.isArray(edit.main) || edit.main.length === 0) {
    err('main', '"main" must be a non-empty list: what is on screen, in order.');
  } else {
    const ids = new Set<string>();
    edit.main.forEach((item, i) => {
      const where = `main[${i}]`;
      if (!isObj(item)) return err(where, 'Each main item is an object naming what to show and until when.');
      unknownKeys(item, MAIN_KEYS, where);
      if (item.layout !== undefined) {
        if (SOURCES.some((k) => item[k] !== undefined)) err(where, 'An item is either a layout or a single thing to show, not both.');
        layout(item.layout, `${where}.layout`);
      } else clip(item, where);
      if (item.id !== undefined) {
        if (!isStr(item.id)) err(`${where}.id`, 'id must be a non-empty string.');
        else if (ids.has(item.id)) err(`${where}.id`, `Item id "${item.id}" is used twice.`);
        else ids.add(item.id);
      }
      if (item.until !== undefined && item.for !== undefined) err(where, 'Give "until" or "for", not both.');
      if (item.until !== undefined) anchor(item.until, `${where}.until`);
      if (item.for !== undefined && (!isNum(item.for) || item.for <= 0)) err(`${where}.for`, '"for" is a duration in seconds.');
      if (item.enter !== undefined) {
        const e = item.enter;
        const type = typeof e === 'string' ? e : isObj(e) ? e.type : null;
        if (!ENTER_TYPES.includes(type as string)) err(`${where}.enter`, `enter is one of: ${ENTER_TYPES.join(', ')}.`);
        if (isObj(e) && e.frames !== undefined && (!Number.isInteger(e.frames) || (e.frames as number) < 1 || (e.frames as number) > 30)) {
          err(`${where}.enter.frames`, 'frames is a whole number from 1 to 30.');
        }
      }
    });
  }

  if (edit.overlays !== undefined) {
    if (!Array.isArray(edit.overlays)) err('overlays', 'overlays must be a list.');
    else {
      edit.overlays.forEach((ov, i) => {
        const where = `overlays[${i}]`;
        if (!isObj(ov)) return err(where, 'Each overlay is an object like {"tag": "up to 4x", "from": {...}, "until": {...}}.');
        unknownKeys(ov, OVERLAY_KEYS, where);
        const kinds = OVERLAY_KINDS.filter((k) => ov[k] !== undefined);
        if (kinds.length !== 1) err(where, `An overlay is exactly one of: ${OVERLAY_KINDS.join(', ')}.`);
        else if (!isStr(ov[kinds[0]])) err(`${where}.${kinds[0]}`, `${kinds[0]} must be a non-empty string.`);
        if (ov.props !== undefined && !isObj(ov.props)) err(`${where}.props`, 'props must be an object.');
        if (ov.from !== undefined) anchor(ov.from, `${where}.from`);
        if (ov.until !== undefined && ov.for !== undefined) err(where, 'Give "until" or "for", not both.');
        if (ov.until !== undefined) anchor(ov.until, `${where}.until`);
        if (ov.for !== undefined && (!isNum(ov.for) || ov.for <= 0)) err(`${where}.for`, '"for" is a duration in seconds.');
        if (ov.zone !== undefined && !ZONES.includes(ov.zone as string)) err(`${where}.zone`, `zone is one of: ${ZONES.join(', ')}.`);
      });
    }
  }

  if (edit.captions !== undefined) {
    if (!isObj(edit.captions)) err('captions', 'captions must be an object like {"variant": "pop", "zone": "lower"}.');
    else {
      const c = edit.captions;
      unknownKeys(c, ['enabled', 'variant', 'zone', 'maxWords', 'maxChars', 'phrases'], 'captions');
      if (c.phrases !== undefined && (!Array.isArray(c.phrases) || c.phrases.some((x) => !isStr(x)))) err('captions.phrases', 'phrases is a list of the caption phrases, in speaking order.');
      if (c.enabled !== undefined && typeof c.enabled !== 'boolean') err('captions.enabled', 'enabled is true or false.');
      if (c.zone !== undefined && !ZONES.includes(c.zone as string)) err('captions.zone', `zone is one of: ${ZONES.join(', ')}.`);
      for (const k of ['maxWords', 'maxChars']) {
        if (c[k] !== undefined && (!Number.isInteger(c[k]) || (c[k] as number) < 1)) err(`captions.${k}`, `${k} is a whole number above 0.`);
      }
    }
  }
  for (const key of ['music', 'audio']) {
    if (edit[key] !== undefined && !isObj(edit[key])) err(key, `${key} must be an object.`);
  }
  return out;
}
