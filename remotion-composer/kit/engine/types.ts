// The edit a director writes (composition/edit.json) and the timeline the engine renders from it.
// Pure types: this file and its sibling .ts modules load in Node (the kit command) and in the
// browser (the render), so they never import React or Remotion.

export type Edge = 'start' | 'end';

/** A moment in the video. A bare number is seconds. */
export type Anchor =
  | number
  | {t: number; offset?: number}
  | {word: string; line?: string; nth?: number; edge?: Edge; offset?: number}
  | {line: string; edge?: Edge; offset?: number}
  | {voice: Edge; offset?: number}
  | {item: string; edge?: Edge; offset?: number}
  | {end: true; offset?: number};

export type EnterSpec = string | {type: string; frames?: number};
export type MotionSpec = string | {type?: string; from?: number; to?: number; origin?: string};

/** What fills a frame, or one slot of a layout. Exactly one source key is set. */
export type ClipSource = {
  take?: string;
  video?: string;
  image?: string;
  scene?: string;
  freeze?: string;
  color?: string;
  props?: Record<string, unknown>;
  motion?: MotionSpec;
  fit?: 'cover' | 'contain' | 'blur';
  /** Seconds into a video where playback starts (not for takes: those follow the voice). */
  start?: number;
  speed?: number;
  /** Keep the clip's own sound at this volume (0-1). Clips are silent by default. */
  audio?: number;
};

export type LayoutSpec =
  | {type: 'split'; ratio?: number; direction?: 'vertical' | 'horizontal'; slots: ClipSource[]}
  | {type: 'pip'; base: ClipSource; inset: ClipSource; corner?: string; size?: number}
  | {type: 'card'; media: ClipSource; aspect?: number; width?: number; top?: number; backdrop?: ClipSource}
  | {type: 'grid'; columns?: number; slots: ClipSource[]};

/** One entry of the main sequence. It starts where the previous one ended. */
export type MainItem = ClipSource & {
  id?: string;
  layout?: LayoutSpec;
  until?: Anchor;
  for?: number;
  enter?: EnterSpec;
};

export type OverlayItem = {
  id?: string;
  tag?: string;
  card?: string;
  text?: string;
  scene?: string;
  props?: Record<string, unknown>;
  from?: Anchor;
  until?: Anchor;
  for?: number;
  zone?: ZoneName;
};

export type ZoneName = 'upper' | 'center' | 'lower' | 'full';

/** `take`: a presenter take is filmed for this line (true, or the path of the take).
 *  `audio`: the recording of the line when it is not at assets/audio/<id>.mp3. */
export type VoiceLineSpec = {id: string; text: string; take?: boolean | string; audio?: string; speaker?: string};

export type CaptionSpec = {
  enabled?: boolean;
  variant?: string;
  zone?: ZoneName;
  maxWords?: number;
  maxChars?: number;
  /** The exact phrases to show, in speaking order, when the director wants to set the breaks
   *  (and the spelling) by hand. Words not covered fall back to the automatic grouping. */
  phrases?: string[];
};

export type Edit = {
  canvas?: {size?: string; fps?: number; safe?: string};
  target_seconds?: number;
  voice?: {lines: VoiceLineSpec[]; gap?: number; language?: string};
  music?: {file?: string; volume?: number; fade_out?: number};
  audio?: {mix?: string};
  captions?: CaptionSpec;
  main: MainItem[];
  overlays?: OverlayItem[];
};

// ---- voice timings (work/kit/voice.json, written by `kit voice`; estimated from the text before that)

export type Word = {text: string; start: number; end: number};
export type VoicePart = {id: string; start: number; dur: number; audio?: string; take?: string; words: Word[]};
/** `text` is the script the timings were made from; a changed script means the timings are stale. */
export type VoiceLine = {id: string; text?: string; parts: VoicePart[]};
export type Voice = {lines: VoiceLine[]; estimated?: boolean};

// ---- the resolved timeline (frames; the render's only input)

export type Rect = {x: number; y: number; w: number; h: number};
export type Canvas = {width: number; height: number; fps: number};

export type ResolvedEnter = {type: string; frames: number};
/** Slow camera move over the item: scale and shift (percent of the frame) from start to end. */
export type ResolvedMotion = {scale: [number, number]; x: [number, number]; y: [number, number]; origin: string};

export type ResolvedClip = {
  kind: 'take' | 'video' | 'image' | 'scene' | 'freeze' | 'color' | 'layout';
  src?: string;
  /** Frames to skip at the head of the source. */
  trimBefore?: number;
  /** Freeze a video on this source frame. */
  freezeFrame?: number;
  scene?: string;
  color?: string;
  props?: Record<string, unknown>;
  motion: ResolvedMotion | null;
  fit: 'cover' | 'contain' | 'blur';
  speed: number;
  volume: number;
  /** The file is not there yet (draft): the render shows a labelled placeholder. */
  missing?: boolean;
  layout?: ResolvedLayout;
};

export type ResolvedLayout =
  | {type: 'split'; ratio: number; direction: 'vertical' | 'horizontal'; slots: ResolvedClip[]}
  | {type: 'pip'; base: ResolvedClip; inset: ResolvedClip; corner: string; size: number}
  | {type: 'card'; media: ResolvedClip; aspect: number; width: number; top: number | null; backdrop: ResolvedClip | null}
  | {type: 'grid'; columns: number; slots: ResolvedClip[]};

export type ResolvedItem = ResolvedClip & {
  id: string;
  from: number;
  to: number;
  /** Extra frames this item stays alive underneath the next item's entrance. */
  tail: number;
  enter: ResolvedEnter;
  label: string;
};

export type ResolvedOverlay = {
  id: string;
  kind: 'tag' | 'card' | 'text' | 'scene';
  name: string;
  text?: string;
  props: Record<string, unknown>;
  from: number;
  to: number;
  zone: ZoneName | null;
};

export type CaptionWord = {text: string; from: number; to: number};
export type CaptionGroup = {from: number; to: number; words: CaptionWord[]; spaced: boolean; rtl: boolean};

export type Diag = {level: 'error' | 'warning' | 'info'; code: string; message: string; where?: string};

export type Resolved = {
  version: 1;
  fps: number;
  width: number;
  height: number;
  durationInFrames: number;
  voiceEndFrame: number;
  safe: {top: number; bottom: number; left: number; right: number};
  zones: Record<ZoneName, Rect>;
  main: ResolvedItem[];
  overlays: ResolvedOverlay[];
  captions: {enabled: boolean; variant: string | null; zone: ZoneName; groups: CaptionGroup[]};
  audio: {mix: string | null};
  /** Assets the edit names that are not on disk yet. */
  pending: string[];
  voiceEstimated: boolean;
  debug?: boolean;
  diagnostics: Diag[];
};

export const MIN_ITEM_FRAMES = 4;
export const TAKE_LIMIT_SECONDS = 14.5;
export const BUILTIN_SCENES = ['BeforeAfter', 'KineticText', 'ListReveal', 'QuoteCard', 'ScreenDemo', 'StatCount'];
