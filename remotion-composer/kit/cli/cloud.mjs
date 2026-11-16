#!/usr/bin/env node
// The final draw of a kit video on Remotion Lambda instead of on this machine.
//
//   node cloud.mjs --project DIR --name NAME
//
// `kit render` has already checked the edit and staged the project's media (work/kit/public)
// and the composition's input (work/kit/cloud_props.json). This uploads the composition with that
// media as a Remotion "site", draws it on Lambda, downloads the video to renders/<NAME>_raw.mp4
// and removes what it uploaded. `kit render` then finishes the file exactly as after a local draw.
//
// It needs the network and the AWS credentials, so in a job it never runs inside the agent's
// box: the paid-tools desk runs it (tools/video/cloud_render.py). One JSON line is printed last.
//
// Settings (the server's environment): REMOTION_AWS_ACCESS_KEY_ID and
// REMOTION_AWS_SECRET_ACCESS_KEY, REMOTION_AWS_REGION (us-east-1 when unset), and optionally
// KIT_CLOUD_FRAMES_PER_FUNCTION (more frames each means fewer functions for one video).
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const args = process.argv.slice(2);
const option = (name, fallback = null) => {
  const at = args.indexOf(`--${name}`);
  return at >= 0 && at + 1 < args.length ? args[at + 1] : fallback;
};
const KIT_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const project = path.resolve(option('project', process.cwd()));
const comp = path.join(project, 'composition');
const work = path.join(project, 'work', 'kit');
const name = (option('name', 'video') || 'video').replace(/[^A-Za-z0-9_.-]+/g, '_').replace(/\.mp4$/i, '');
const raw = path.join(project, 'renders', `${name}_raw.mp4`);
const region = process.env.REMOTION_AWS_REGION || 'us-east-1';
// A site left behind by a call that was killed is removed by a later call once it is this old.
const STALE_SITE_MS = 2 * 60 * 60 * 1000;
const SITE_PREFIX = 'kit-';

const facts = {ok: false, region};
const finish = (code) => {
  console.log(JSON.stringify(facts));
  process.exit(code);
};

let lambda;
try {
  lambda = await import('@remotion/lambda');
} catch (err) {
  facts.error = `@remotion/lambda is not installed: ${String(err?.message ?? err).slice(0, 200)}`;
  finish(2);
}
if (!process.env.REMOTION_AWS_ACCESS_KEY_ID || !process.env.REMOTION_AWS_SECRET_ACCESS_KEY) {
  facts.error = 'REMOTION_AWS_ACCESS_KEY_ID and REMOTION_AWS_SECRET_ACCESS_KEY are not set';
  finish(2);
}
const propsFile = path.join(work, 'cloud_props.json');
if (!fs.existsSync(propsFile) || !fs.existsSync(path.join(work, 'public'))) {
  facts.error = 'the project is not staged for a cloud draw (run `kit render`, which stages it)';
  finish(2);
}
const inputProps = JSON.parse(fs.readFileSync(propsFile, 'utf8'));

let bucketName = null;
let siteName = null;
let renderId = null;
try {
  let mark = Date.now();
  ({bucketName} = await lambda.getOrCreateBucket({region, enableFolderExpiry: true}));
  const functions = await lambda.getFunctions({region, compatibleOnly: true});
  if (!functions.length) throw new Error(`no Remotion Lambda function of this version is deployed in ${region}`);
  const functionName = functions[0].functionName;

  // Leftovers of calls that never reached their own clean-up.
  try {
    const {sites} = await lambda.getSites({region});
    const old = sites.filter((site) => site.id.startsWith(SITE_PREFIX) && Date.now() - Number(site.lastModified ?? 0) > STALE_SITE_MS);
    for (const site of old.slice(0, 5)) await lambda.deleteSite({bucketName: site.bucketName, siteName: site.id, region});
  } catch {}

  siteName = `${SITE_PREFIX}${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  const site = await lambda.deploySite({
    entryPoint: path.join(comp, 'src', 'index.ts'), bucketName, region, siteName,
    options: {
      rootDir: comp, publicDir: path.join(work, 'public'), enableCaching: false,
      webpackOverride: (config) => ({...config, resolve: {...config.resolve, alias: {...(config.resolve?.alias ?? {}), '@kit': KIT_DIR}}}),
    },
  });
  facts.upload_seconds = +((Date.now() - mark) / 1000).toFixed(1);

  mark = Date.now();
  const perFunction = Number(process.env.KIT_CLOUD_FRAMES_PER_FUNCTION || 0);
  const started = await lambda.renderMediaOnLambda({
    region, functionName, serveUrl: site.serveUrl, composition: 'Video', inputProps,
    codec: 'h264', crf: 18, pixelFormat: 'yuv420p', colorSpace: 'bt709',
    privacy: 'private', maxRetries: 1, logLevel: 'error', deleteAfter: '1-day',
    ...(perFunction >= 4 ? {framesPerLambda: Math.round(perFunction)} : {}),
  });
  renderId = started.renderId;
  let progress;
  for (;;) {
    progress = await lambda.getRenderProgress({renderId, bucketName: started.bucketName, functionName, region});
    if (progress.fatalErrorEncountered) {
      const first = (progress.errors ?? [])[0];
      throw new Error(`the draw failed on Lambda: ${String(first?.message ?? JSON.stringify(progress.errors)).slice(0, 900)}`);
    }
    if (progress.done) break;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  facts.render_seconds = +((Date.now() - mark) / 1000).toFixed(1);
  facts.functions = progress.lambdasInvoked ?? null;
  facts.cost_usd = Number(progress.costs?.accruedSoFar ?? 0);

  mark = Date.now();
  fs.mkdirSync(path.dirname(raw), {recursive: true});
  await lambda.downloadMedia({bucketName: started.bucketName, renderId, region, outPath: raw});
  facts.download_seconds = +((Date.now() - mark) / 1000).toFixed(1);
  facts.file = path.relative(project, raw);
  facts.ok = true;
} catch (err) {
  facts.error = String(err?.message ?? err).split('\n').slice(0, 4).join(' ').slice(0, 1200);
} finally {
  // The upload holds the video's media: it is removed whatever happened. The rendered file too
  // (it also expires by itself a day later).
  if (bucketName && siteName) {
    try {
      await lambda.deleteSite({bucketName, siteName, region});
    } catch (err) {
      facts.left_behind = `site ${siteName}`;
    }
  }
  if (bucketName && renderId) {
    try {
      await lambda.deleteRender({bucketName, renderId, region});
    } catch {}
  }
}
finish(facts.ok ? 0 : 1);
