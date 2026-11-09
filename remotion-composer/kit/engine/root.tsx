// A job's entry file is three lines: registerRoot(makeRoot({look, scenes})).
// The timeline arrives as input props from the kit command, already resolved to frames.

import React from 'react';
import {Composition} from 'remotion';
import {builtinScenes} from '../scenes/index.ts';
import type {Look, SceneRegistry} from './context.tsx';
import type {Resolved} from './types.ts';
import {zonesFor, safeArea} from './zones.ts';
import {KitVideo} from './Video.tsx';

const EMPTY: Resolved = {
  version: 1,
  fps: 30,
  width: 1080,
  height: 1920,
  durationInFrames: 30,
  voiceEndFrame: 0,
  safe: safeArea(1080, 1920, 'tiktok'),
  zones: zonesFor(1080, 1920, 'tiktok'),
  main: [],
  overlays: [],
  captions: {enabled: false, variant: null, zone: 'lower', groups: []},
  audio: {mix: null},
  pending: [],
  voiceEstimated: false,
  diagnostics: [],
};

export const COMPOSITION_ID = 'Video';

export function makeRoot(opts: {look: Look; scenes?: SceneRegistry}): React.FC {
  const scenes: SceneRegistry = {...builtinScenes, ...(opts.scenes ?? {})};
  const Main: React.FC<{resolved: Resolved}> = ({resolved}) => <KitVideo resolved={resolved} look={opts.look} scenes={scenes} />;
  return () => (
    <Composition
      id={COMPOSITION_ID}
      component={Main}
      width={1080}
      height={1920}
      fps={30}
      durationInFrames={30}
      defaultProps={{resolved: EMPTY}}
      calculateMetadata={({props}) => ({
        durationInFrames: Math.max(1, props.resolved.durationInFrames),
        fps: props.resolved.fps,
        width: props.resolved.width,
        height: props.resolved.height,
      })}
    />
  );
}
