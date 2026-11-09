// Ready-made scenes. An edit names them: {"scene": "BeforeAfter", "props": {...}}.
// A job's own scenes (composition/src/scenes.ts) are added to these and may replace one.

import type {SceneRegistry} from '../engine/context.tsx';
import {BeforeAfter} from './BeforeAfter.tsx';

export {BeforeAfter, BeforeAfterCore} from './BeforeAfter.tsx';
export type {BeforeAfterProps, ChromeProps} from './BeforeAfter.tsx';

export const builtinScenes: SceneRegistry = {BeforeAfter};
