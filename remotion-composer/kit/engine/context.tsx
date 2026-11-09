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

export type Look = {
  name: string;
  /** Colours, type and shape: the first thing a job restyles. */
  tokens: Record<string, unknown>;
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

/** The frame (local to the current shot) at which the voice says a word, or null. */
export const useWordFrame = (word: string, nth = 1): number | null => {
  const {resolved} = useKit();
  const shot = useShot();
  const want = word.normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');
  let seen = 0;
  for (const group of resolved.captions.groups) {
    for (const w of group.words) {
      if (w.text.normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '') === want && w.from >= shot.from) {
        seen += 1;
        if (seen === nth) return w.from - shot.from;
      }
    }
  }
  return null;
};

/** Extra frames an overlay stays mounted after its end, for its exit animation. */
export const EXIT_FRAMES = 8;
