// Checks on a resolved timeline that need no rendering: gaps, flashes of text nobody can read,
// things fighting for the same part of the frame, unreadable colour pairs.

import {contrastRatio} from './color.ts';
import type {Diag, Edit, Resolved, ZoneName} from './types.ts';

export type LookMeta = {
  /** Where each card sits when the edit does not say. */
  cardZones?: Record<string, ZoneName>;
  /** Text and background colour pairs the look draws, for the legibility check. */
  contrastPairs?: Array<{name: string; fg: string; bg: string}>;
};

const READ_BASE = 0.3;
const READ_PER_WORD = 0.25;

export function lintResolved(resolved: Resolved, edit: Edit, meta: LookMeta = {}): Diag[] {
  const out: Diag[] = [];
  const d = (level: Diag['level'], code: string, message: string, where?: string) => out.push({level, code, message, where});
  const sec = (frames: number) => (frames / resolved.fps).toFixed(2);

  // The main sequence must cover every frame.
  let expect = 0;
  for (const item of resolved.main) {
    if (item.from !== expect) d('error', 'coverage-gap', `Nothing is on screen from ${sec(expect)} s to ${sec(item.from)} s.`, item.id);
    expect = item.to;
    if (item.to - item.from < 12 && item.kind !== 'freeze') {
      d('warning', 'shot-very-short', `"${item.label}" is on screen for ${item.to - item.from} frames (${sec(item.from)} to ${sec(item.to)} s); a viewer barely registers it.`, item.id);
    }
  }
  if (!resolved.main.length) d('error', 'main-empty', 'Nothing resolved into the main sequence.');
  else if (expect !== resolved.durationInFrames) d('error', 'coverage-gap', `The main sequence ends at ${sec(expect)} s but the video runs ${sec(resolved.durationInFrames)} s.`);

  const zoneOf = (o: Resolved['overlays'][number]): ZoneName =>
    o.zone ?? (o.kind === 'card' ? meta.cardZones?.[o.name] : undefined) ?? 'upper';

  for (const o of resolved.overlays) {
    if (o.kind === 'tag' || o.kind === 'text') {
      const words = (o.text ?? '').split(/\s+/).filter((w) => /[\p{L}\p{N}]/u.test(w)).length;
      const need = READ_BASE + READ_PER_WORD * words;
      const have = (o.to - o.from) / resolved.fps;
      if (have + 1e-6 < need) d('warning', 'text-too-brief', `"${o.text}" is on screen ${have.toFixed(2)} s; ${words} word(s) need about ${need.toFixed(2)} s to be read.`, o.id);
    }
    if (resolved.captions.enabled && zoneOf(o) === resolved.captions.zone && o.to > 0 && o.from < resolved.voiceEndFrame) {
      d('error', 'overlay-in-caption-zone', `"${o.text ?? o.name}" sits in the ${resolved.captions.zone} zone, where the captions are. Move it to another zone.`, o.id);
    }
  }
  for (let i = 0; i < resolved.overlays.length; i++) {
    for (let j = i + 1; j < resolved.overlays.length; j++) {
      const a = resolved.overlays[i];
      const b = resolved.overlays[j];
      const za = zoneOf(a);
      const zb = zoneOf(b);
      const sameArea = za === zb || za === 'full' || zb === 'full';
      const overlap = Math.min(a.to, b.to) - Math.max(a.from, b.from);
      if (sameArea && overlap > 3) {
        d('warning', 'overlay-overlap', `"${a.text ?? a.name}" and "${b.text ?? b.name}" share the ${za === 'full' ? zb : za} zone for ${sec(overlap)} s (${sec(Math.max(a.from, b.from))} to ${sec(Math.min(a.to, b.to))} s). End the first before the second starts, or move one.`, `${a.id},${b.id}`);
      }
    }
  }

  if (typeof edit.target_seconds === 'number') {
    const total = resolved.durationInFrames / resolved.fps;
    if (Math.abs(total - edit.target_seconds) > 1.5) {
      d('warning', 'duration-off-target', `The video runs ${total.toFixed(1)} s; the target is ${edit.target_seconds} s.`);
    }
  }

  for (const pair of meta.contrastPairs ?? []) {
    const ratio = contrastRatio(pair.fg, pair.bg);
    // 1.8 is lenient on purpose: heavy display type on a saturated card reads at about 2
    // (coral on yellow, accepted in a shipped video, measures 2.1); white on yellow (1.4) does not.
    if (ratio !== null && ratio < 1.8) {
      d('warning', 'low-contrast', `${pair.name}: ${pair.fg} on ${pair.bg} has a contrast of ${ratio.toFixed(1)}; under 1.8 is hard to read even in heavy type. Change one of the two colours.`, 'look');
    }
  }
  return out;
}
