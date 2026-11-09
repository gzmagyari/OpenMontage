// What a look is, and what scenes and look components can ask the engine.

import React from 'react';
import type {CaptionGroup, Canvas, Rect, Resolved, ZoneName} from './types.ts';

export type CaptionProps = {
  group: CaptionGroup;
  /** Index of the word being spoken. */
  active: number;
  /** Frames since this phrase appeared. */
  localFrame: number;
  /** The current frame of the video. */
  frame: number;
  zone: Rect;
  variant: string | null;
};

/** `frames` is how long the overlay is meant to be on screen; it is kept mounted a few frames
 *  longer (EXIT_FRAMES) so it can animate out. */
export type TagProps = {text: string; props: Record<string, unknown>; zone: Rect; frames: number};
export type CardProps = {props: Record<string, unknown>; zone: Rect; frames: number};

export type SpringFeel = {damping: number; stiffness: number; mass: number};

/** The roles every look names, so ready-made scenes can dress themselves in any look.
 *  A look may carry more (its own caption sizes, extra colours, ...). */
export type LookTokens = {
  color: {ink: string; paper: string; accent: string; negative?: string; positive?: string; [name: string]: string | undefined};
  font: {display: string; body?: string; weight?: number; [name: string]: string | number | undefined};
  /** `shadow: "hard"` is an offset block shadow with a border; "soft" a drop shadow. Sizes are for a 1080-wide frame. */
  shape?: {radius?: number; border?: number; offset?: number; shadow?: 'hard' | 'soft' | 'none'};
  [group: string]: unknown;
};

export type Look = {
  name: string;
  /** Colours, type and shape: the first thing a job restyles. */
  tokens: LookTokens;
  background?: string;
  Caption: React.FC<CaptionProps>;
  Tag: React.FC<TagProps>;
  Text?: React.FC<TagProps>;
  cards: Record<string, React.FC<CardProps>>;
  /** Where each card sits when the edit does not say. */
  cardZones?: Record<string, ZoneName>;
  /** Drawn over shots and overlays, under the captions (vignette, grain, frame). */
  Finish?: React.FC;
  /** A frame around media in card and picture-in-picture layouts. */
  Frame?: React.FC<{children: React.ReactNode; style?: React.CSSProperties}>;
  /** Look-specific pieces of ready-made scenes (for example the before/after handle). */
  parts?: Record<string, React.FC<any>>;
  motion?: {enter?: SpringFeel};
};

export type SceneRegistry = Record<string, React.FC<any>>;

export type KitValue = {resolved: Resolved; look: Look; scenes: SceneRegistry; canvas: Canvas};

export const KitContext = React.createContext<KitValue | null>(null);

export const useKit = (): KitValue => {
  const value = React.useContext(KitContext);
  if (!value) throw new Error('This component must be rendered inside the kit video.');
  return value;
};
export const useLook = (): Look => useKit().look;
export const useCanvas = (): Canvas => useKit().canvas;

/** The main item a scene is rendered in: `frames` is its time on screen, `from` its first frame. */
export type ShotValue = {id: string; frames: number; from: number};
export const ShotContext = React.createContext<ShotValue>({id: '', frames: 1, from: 0});
export const useShot = (): ShotValue => React.useContext(ShotContext);

const fold = (s: string): string => s.normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');

/** The frame, counted from `from`, at which the voice next says a word; null when it does not. */
export const wordFrame = (resolved: Resolved, from: number, word: string, nth = 1): number | null => {
  const want = fold(word);
  let seen = 0;
  for (const group of resolved.captions.groups) {
    for (const w of group.words) {
      if (fold(w.text) === want && w.from >= from) {
        seen += 1;
        if (seen === nth) return w.from - from;
      }
    }
  }
  return null;
};

/** The frame (local to the current shot or overlay) at which the voice says a word, or null. */
export const useWordFrame = (word: string, nth = 1): number | null => wordFrame(useKit().resolved, useShot().from, word, nth);

/** Extra frames an overlay stays mounted after its end, for its exit animation. */
export const EXIT_FRAMES = 8;
