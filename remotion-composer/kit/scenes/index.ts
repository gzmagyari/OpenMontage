// Ready-made scenes. An edit names them: {"scene": "BeforeAfter", "props": {...}}.
// A job's own scenes (composition/src/scenes.ts) are added to these and may replace one.
// Each dresses itself from the job's look (colours, type, the feel of a surface).

import type {SceneRegistry} from '../engine/context.tsx';
import {BeforeAfter} from './BeforeAfter.tsx';
import {KineticText} from './KineticText.tsx';
import {ListReveal} from './ListReveal.tsx';
import {QuoteCard} from './QuoteCard.tsx';
import {ScreenDemo} from './ScreenDemo.tsx';
import {StatCount} from './StatCount.tsx';

export {Backdrop} from './Backdrop.tsx';
export type {BackdropSpec} from './Backdrop.tsx';
export {BeforeAfter, BeforeAfterCore} from './BeforeAfter.tsx';
export type {BeforeAfterProps, ChromeProps} from './BeforeAfter.tsx';
export {KineticText} from './KineticText.tsx';
export {ListReveal} from './ListReveal.tsx';
export {QuoteCard} from './QuoteCard.tsx';
export {ScreenDemo} from './ScreenDemo.tsx';
export type {DemoStep} from './ScreenDemo.tsx';
export {StatCount} from './StatCount.tsx';

// Keep in step with BUILTIN_SCENES in engine/types.ts (a test compares the two).
export const builtinScenes: SceneRegistry = {BeforeAfter, KineticText, ListReveal, QuoteCard, ScreenDemo, StatCount};
