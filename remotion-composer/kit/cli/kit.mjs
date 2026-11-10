#!/usr/bin/env node
// The render side of the `kit` command: check an edit, make a check sheet, render the video.
// Usage: node kit.mjs <check|sheet|render|resolve> [--project DIR] [--draft] [--frames 10,200]
//        [--name NAME] [--allow-starter-look] [--json]
// (`kit voice`, `kit mix`, `kit init` and `kit look` live in lib/videokit.)

import {spawnSync} from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {chromeTempDir, withTempDir} from '../engine/browserTemp.ts';
import {lintResolved} from '../engine/lint.ts';
import {estimateVoice, resolveEdit} from '../engine/resolve.ts';
import {validateEdit} from '../engine/schema.ts';
import {BUILTIN_SCENES} from '../engine/types.ts';
import {overlaps} from '../engine/zones.ts';

const KIT_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const MEDIA_RE = /\.(png|jpe?g|webp|gif|svg|mp4|mov|webm|m4v|mp3|wav|m4a|aac|ogg)$/i;
const VIDEO_RE = /\.(mp4|mov|webm|m4v)$/i;
const AUDIO_RE = /\.(mp3|wav|m4a|aac|ogg)$/i;
// How far the soundtrack's length may differ from the video's before it counts as out of date.
const MIX_SLACK_SECONDS = 0.15;
// A file changed this recently may still be on its way in (a draft treats it as not there yet).
const LANDING_MS = 3000;
const SHEET_COLUMNS = 6;
const SHEET_FRAMES = 12;
const TARGET_LUFS = -14;

// ---------------------------------------------------------------- arguments

const argv = process.argv.slice(2);
const command = argv[0];
const flag = (name) => argv.includes(`--${name}`);
const option = (name, fallback = null) => {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : fallback;
};
const project = path.resolve(option('project', process.cwd()));
const comp = path.join(project, 'composition');
const work = path.join(project, 'work', 'kit');
const draft = flag('draft');

const started = Date.now();
const lap = (label, since) => console.log(`  ${label}: ${((Date.now() - since) / 1000).toFixed(1)} s`);
// Chrome's own temp folder while a browser is open, when it is in memory, and which kind the
// last browser got ('memory' or 'disk'): see openStage.
let browserTemp = null;
let browserTempKind = 'disk';
const fail = (message) => {
  console.error(`kit: ${message}`);
  process.exit(2);
};

// ---------------------------------------------------------------- reading the job

function readJson(file, what) {
  let text;
  try {
    text = fs.readFileSync(file, 'utf8');
  } catch {
    return null;
  }
  try {
    return JSON.parse(text);
  } catch (err) {
    fail(`${what} (${path.relative(project, file)}) is not valid JSON: ${err.message}`);
  }
}

function probeDuration(file) {
  const r = spawnSync('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', file], {encoding: 'utf8'});
  const n = Number((r.stdout || '').trim());
  return Number.isFinite(n) && n > 0 ? n : undefined;
}

/** Every media path named inside scene or overlay props ("pair" names a _before/_after pair). */
function propPaths(props, out = new Set()) {
  if (typeof props === 'string') {
    if (MEDIA_RE.test(props) && !/^https?:/.test(props)) out.add(props);
  } else if (Array.isArray(props)) props.forEach((v) => propPaths(v, out));
  else if (props && typeof props === 'object') {
    for (const [key, value] of Object.entries(props)) {
      if (key === 'pair' && typeof value === 'string') {
        out.add(`${value}_before.jpg`);
        out.add(`${value}_after.jpg`);
      } else propPaths(value, out);
    }
  }
  return out;
}

function clipSources(item, visit) {
  visit(item);
  const l = item.layout;
  if (!l) return;
  if (Array.isArray(l.slots)) l.slots.forEach(visit);
  for (const key of ['base', 'inset', 'media', 'backdrop']) if (l[key]) visit(l[key]);
}

async function lookMeta(diags) {
  const tokensFile = path.join(comp, 'src', 'look', 'tokens.ts');
  if (!fs.existsSync(tokensFile)) {
    diags.push({level: 'error', code: 'look-missing', message: 'composition/src/look/tokens.ts is missing. Run `kit look <name>` to copy a starter look.', where: 'look'});
    return {};
  }
  const body = fs.readFileSync(tokensFile, 'utf8');
  const digest = (s) => crypto.createHash('sha256').update(s.replace(/\r\n/g, '\n')).digest('hex');
  const looksDir = path.join(KIT_DIR, 'looks');
  for (const name of fs.readdirSync(looksDir)) {
    const starter = path.join(looksDir, name, 'tokens.ts');
    if (fs.existsSync(starter) && digest(fs.readFileSync(starter, 'utf8')) === digest(body) && !flag('allow-starter-look')) {
      diags.push({
        level: draft ? 'warning' : 'error',
        code: 'look-untouched',
        message: `The look is still the untouched "${name}" starter. Restyle composition/src/look/tokens.ts for this video (colours and type at least) before the final render.`,
        where: 'look',
      });
    }
  }
  try {
    const mod = await import(`${pathToFileURL(tokensFile).href}?v=${Date.now()}`);
    return mod.meta ?? {};
  } catch (err) {
    diags.push({level: 'error', code: 'look-broken', message: `composition/src/look/tokens.ts does not load: ${String(err.message).split('\n')[0]}`, where: 'look'});
    return {};
  }
}

function customScenes() {
  const file = path.join(comp, 'src', 'scenes.ts');
  if (!fs.existsSync(file)) return [];
  const m = /export const scenes[^=]*=\s*\{([^}]*)\}/s.exec(fs.readFileSync(file, 'utf8'));
  if (!m) return [];
  return m[1].split(',').map((s) => s.split(':')[0].replace(/\/\/.*$/gm, '').trim()).filter(Boolean);
}

/** The known name a misspelt one was probably meant to be (same letters, or one or two off). */
function nearName(name, known) {
  const fold = (s) => String(s).toLowerCase().replace(/[^a-z0-9]/g, '');
  const distance = (a, b) => {
    const row = Array.from({length: b.length + 1}, (_, j) => j);
    for (let i = 1; i <= a.length; i++) {
      let diagonal = row[0];
      row[0] = i;
      for (let j = 1; j <= b.length; j++) {
        const above = row[j];
        row[j] = Math.min(row[j] + 1, row[j - 1] + 1, diagonal + (a[i - 1] === b[j - 1] ? 0 : 1));
        diagonal = above;
      }
    }
    return row[b.length];
  };
  const want = fold(name);
  return [...known].find((k) => fold(k) === want || (want.length > 4 && distance(fold(k), want) <= 2)) ?? null;
}

const mtime = (file) => {
  try {
    return fs.statSync(file).mtimeMs;
  } catch {
    return null;
  }
};

/** True while a file is, as far as one can tell, still being written: empty, or changed in
 *  the last few seconds. Only a draft asks. */
function landing(file) {
  try {
    const stat = fs.statSync(file);
    return stat.size === 0 || Date.now() - stat.mtimeMs < LANDING_MS;
  } catch {
    return false;
  }
}

/** The soundtrack against the edit it belongs to. A video with a voice or music must have one,
 *  as long as the video and no older than what it was mixed from; otherwise the export would be
 *  silent, or its sound would stop or run on at the wrong moment. */
function soundtrackFindings(edit, resolved, named, pending) {
  const diags = [];
  const scripted = (edit.voice?.lines ?? []).length > 0;
  const music = typeof edit.music?.file === 'string' && edit.music.file.trim() ? edit.music.file : null;
  if (music && !fs.existsSync(path.join(project, music))) {
    if (!pending.has(music) && !draft) diags.push({level: 'error', code: 'asset-missing', message: `${music} does not exist.`, where: 'music.file'});
    pending.add(music);
  }
  const mix = resolved.audio.mix;
  if (!mix) {
    if ((scripted || music) && !draft) {
      diags.push({level: 'error', code: 'mix-missing', message: `The edit has ${scripted ? 'a voice' : 'music'} but no soundtrack was built, so the video would be silent. Run \`kit mix\`, then check again.`, where: 'audio'});
    }
    return diags;
  }
  const file = path.join(project, mix);
  if (!fs.existsSync(file)) return diags;
  const seconds = probeDuration(file);
  const total = resolved.durationInFrames / resolved.fps;
  const stale = (why) => diags.push({
    level: named || draft ? 'warning' : 'error',
    code: 'mix-stale',
    message: `${why} ${named ? `Rebuild ${mix} to match.` : 'Run `kit mix` again.'}`,
    where: 'audio',
  });
  if (seconds !== undefined && Math.abs(seconds - total) > MIX_SLACK_SECONDS) {
    stale(`The soundtrack is ${seconds.toFixed(2)} s long but the video is ${total.toFixed(2)} s.`);
  } else if (!named) {
    // The kit's own mix: built from the voice timings and the music file as they were then.
    const made = mtime(file);
    const voiceAt = scripted ? mtime(path.join(work, 'voice.json')) : null;
    const musicAt = music ? mtime(path.join(project, music)) : null;
    if (made !== null && voiceAt !== null && voiceAt > made + 1000) stale('The voice was timed again after the soundtrack was mixed.');
    else if (made !== null && musicAt !== null && musicAt > made + 1000) stale('The music file changed after the soundtrack was mixed.');
  }
  return diags;
}

/** Everything `check` knows: the resolved timeline and every finding. */
async function prepare() {
  const editFile = path.join(comp, 'edit.json');
  const edit = readJson(editFile, 'The edit');
  if (edit === null) {
    fail(fs.existsSync(comp)
      ? 'composition/edit.json is missing. Write the edit first (or `kit init` to get a skeleton).'
      : `${project} is not a job's project directory (it has no composition/). Run kit there, or pass --project DIR.`);
  }
  const diags = validateEdit(edit);
  if (diags.some((d) => d.level === 'error')) return {edit, diags, resolved: null, meta: {}, unwritten: []};

  const voiceFile = path.join(work, 'voice.json');
  const voice = readJson(voiceFile, 'The voice timings');
  const meta = await lookMeta(diags);

  // Facts about every file the edit can reach.
  const media = {};
  const unreadable = [];
  const note = (rel) => {
    if (!rel || media[rel]) return;
    const abs = path.join(project, rel);
    let exists = fs.existsSync(abs);
    const duration = exists && VIDEO_RE.test(rel) ? probeDuration(abs) : undefined;
    // A draft is made while the paid batch is still writing files: one that is landing right
    // now, or a clip that cannot be read yet, is still generating, not there.
    if (exists && draft && (landing(abs) || (VIDEO_RE.test(rel) && duration === undefined))) exists = false;
    else if (exists && VIDEO_RE.test(rel) && duration === undefined) unreadable.push(rel);
    media[rel] = {exists, duration: exists ? duration : undefined};
  };
  const lines = (voice ?? estimateVoice(edit)).lines ?? [];
  for (const line of lines) for (const part of line.parts) if (part.take) note(part.take);
  for (const item of edit.main) clipSources(item, (c) => [c.video, c.image, MEDIA_RE.test(c.freeze ?? '') ? c.freeze : null].forEach(note));
  if (edit.audio?.mix) note(edit.audio.mix);
  // A soundtrack the edit names is the director's own; one made by `kit mix` is used without
  // being named.
  const mixNamed = Boolean(edit.audio?.mix);
  if (!edit.audio?.mix) {
    const made = ['assets/audio/mix.wav', 'assets/audio/mix.mp3'].find((rel) => fs.existsSync(path.join(project, rel)));
    if (made) edit.audio = {...(edit.audio ?? {}), mix: made};
  }
  if (edit.audio?.mix) note(edit.audio.mix);

  const resolved = resolveEdit({edit, voice, media, draft, captionRules: meta.captions});
  diags.push(...resolved.diagnostics);
  for (const rel of unreadable) diags.push({level: 'error', code: 'asset-unreadable', message: `${rel} cannot be read as a video (it may be incomplete). Generate it again.`, where: rel});

  // Files named in props, and scene names.
  const known = new Set([...BUILTIN_SCENES, ...customScenes()]);
  const pending = new Set(resolved.pending);
  const checkProps = (props, where) => {
    for (const rel of propPaths(props)) {
      const abs = path.join(project, rel);
      if (fs.existsSync(abs) && !(draft && landing(abs))) continue;
      if (!pending.has(rel) && !draft) diags.push({level: 'error', code: 'asset-missing', message: `${rel} does not exist.`, where});
      pending.add(rel);
    }
  };
  // A scene the edit names but nobody has written yet. In a draft that is work still to do, like
  // a file still generating (the frame shows a placeholder), so the plan can be checked and
  // locked before the scene exists; a misspelt name is an error straight away.
  const unwritten = new Set();
  const sceneKnown = (name, where) => {
    if (known.has(name)) return;
    const meant = nearName(name, known);
    const has = `ready-made: ${BUILTIN_SCENES.join(', ')}; registered in composition/src/scenes.ts: ${customScenes().join(', ') || 'none'}`;
    if (meant) diags.push({level: 'error', code: 'scene-unknown', message: `There is no scene "${name}". Did you mean "${meant}"? (${has})`, where});
    else if (draft) {
      if (!unwritten.has(name)) diags.push({level: 'warning', code: 'scene-unwritten', message: `Scene "${name}" is not written yet (${has}). A draft shows a placeholder for it; write composition/src/scenes/${name}.tsx and register it before the final check.`, where});
      unwritten.add(name);
    } else diags.push({level: 'error', code: 'scene-unknown', message: `Scene "${name}" is neither a ready-made scene nor registered in composition/src/scenes.ts (${has}).`, where});
  };
  edit.main.forEach((item, i) => clipSources(item, (c) => {
    if (c.scene === undefined) return;
    checkProps(c.props, `main[${i}]`);
    sceneKnown(c.scene, `main[${i}]`);
  }));
  (edit.overlays ?? []).forEach((ov, i) => {
    checkProps(ov.props, `overlays[${i}]`);
    if (ov.card !== undefined && meta.cardZones && !(ov.card in meta.cardZones)) {
      diags.push({level: 'warning', code: 'card-unknown', message: `Card "${ov.card}" is not listed by this look (it has: ${Object.keys(meta.cardZones).join(', ')}). Add it to the look or use one it has.`, where: `overlays[${i}]`});
    }
    if (ov.scene !== undefined) sceneKnown(ov.scene, `overlays[${i}]`);
  });
  diags.push(...soundtrackFindings(edit, resolved, mixNamed, pending));
  resolved.pending = [...pending].sort();
  diags.push(...lintResolved(resolved, edit, meta));
  return {edit, diags, resolved, meta, unwritten: [...unwritten].sort()};
}

// ---------------------------------------------------------------- reporting

function report(diags, resolved, extra = {}) {
  const count = (level) => diags.filter((d) => d.level === level).length;
  const scripted = (prepared?.edit?.voice?.lines ?? []).length > 0;
  const summary = {
    ok: count('error') === 0,
    errors: count('error'),
    warnings: count('warning'),
    seconds: resolved ? +(resolved.durationInFrames / resolved.fps).toFixed(2) : null,
    shots: resolved ? resolved.main.length : null,
    overlays: resolved ? resolved.overlays.length : null,
    captions: resolved ? resolved.captions.groups.length : null,
    voice: resolved ? (!scripted ? 'none' : resolved.voiceEstimated ? 'estimated from the script' : 'timed') : null,
    pending: resolved ? resolved.pending : [],
    unwritten: prepared?.unwritten ?? [],
    ...extra,
  };
  fs.mkdirSync(work, {recursive: true});
  fs.writeFileSync(path.join(work, 'check.json'), JSON.stringify({summary, diagnostics: diags}, null, 1));
  if (flag('json')) {
    console.log(JSON.stringify({summary, diagnostics: diags}, null, 1));
    return summary;
  }
  for (const level of ['error', 'warning', 'info']) {
    for (const d of diags.filter((x) => x.level === level)) console.log(`${level.toUpperCase().padEnd(7)} ${d.code}${d.where ? ` [${d.where}]` : ''}: ${d.message}`);
  }
  if (resolved) {
    const voice = summary.voice === 'none' ? 'no voice' : `voice ${summary.voice}`;
    console.log(`${summary.ok ? 'OK' : 'NOT OK'}: ${summary.seconds} s, ${summary.shots} shots, ${summary.overlays} overlays, ${summary.captions} caption phrases, ${voice}; ${summary.errors} error(s), ${summary.warnings} warning(s).`);
    if (summary.pending.length) console.log(`${draft ? 'Still generating' : 'MISSING'} (${summary.pending.length}): ${summary.pending.join(', ')}`);
    if (summary.unwritten.length) console.log(`Still to write (${summary.unwritten.length}): ${summary.unwritten.map((name) => `scene ${name}`).join(', ')}`);
  } else console.log(`NOT OK: the edit has ${summary.errors} error(s); fix them and run the check again.`);
  return summary;
}

// ---------------------------------------------------------------- rendering

/** A hard link (instant, no bytes copied) and, across filesystems, a copy. Remotion's file
 *  server refuses symlinked files. */
function place(real, dst) {
  fs.mkdirSync(path.dirname(dst), {recursive: true});
  try {
    fs.linkSync(real, dst);
  } catch {
    fs.copyFileSync(real, dst);
  }
}

/** Draft only: a stand-in for a file a scene or overlay names in its props while that file is
 *  still being generated. The engine swaps a missing shot for a placeholder itself, but a scene
 *  loads its own media, and a file that is not there stops the whole sheet. The stand-in lives
 *  under work/kit/ and is served from the bundle only: the project's own folders are never
 *  touched, so every check still counts the real file as missing. Returns its path, or null. */
function standIn(rel, size) {
  const ext = path.extname(rel).toLowerCase();
  const dir = path.join(work, 'standins');
  const out = path.join(dir, `${crypto.createHash('sha1').update(`${size.width}x${size.height}:${rel}`).digest('hex').slice(0, 16)}${ext}`);
  if (fs.existsSync(out) && fs.statSync(out).size > 0) return out;
  fs.mkdirSync(dir, {recursive: true});
  const run = (args) => spawnSync('ffmpeg', ['-y', '-v', 'error', ...args], {cwd: dir, encoding: 'utf8'}).status === 0 && fs.existsSync(out) && fs.statSync(out).size > 0;
  const shown = rel.length > 46 ? `...${rel.slice(-43)}` : rel;
  if (ext === '.svg') {
    fs.writeFileSync(out, `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 540 960"><rect width="540" height="960" fill="#2a2d34"/><text x="270" y="480" fill="#cfd3da" font-family="DejaVu Sans, sans-serif" font-size="18" text-anchor="middle">waiting for ${shown.replace(/[<&>]/g, '')}</text></svg>`);
    return out;
  }
  if (AUDIO_RE.test(rel)) return run(['-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono', '-t', '180', out]) ? out : null;
  const w = 540;
  const h = Math.max(2, Math.round((w * size.height) / size.width / 2) * 2);
  const font = ['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', '/usr/share/fonts/dejavu/DejaVuSans.ttf'].find((f) => fs.existsSync(f));
  // The words go through a file, so nothing in a path can break the filter's own syntax.
  fs.writeFileSync(path.join(dir, 'label.txt'), `waiting for ${shown}`);
  const label = font ? `,drawtext=fontfile=${font}:textfile=label.txt:fontcolor=0xcfd3da:fontsize=18:x=(w-text_w)/2:y=(h-text_h)/2:box=1:boxcolor=black@0.55:boxborderw=14` : '';
  const picture = `color=c=0x2a2d34:s=${w}x${h}`;
  if (VIDEO_RE.test(rel)) {
    // Long enough for any shot, and tiny: two identical frames a second.
    const made = ext === '.webm'
      ? run(['-f', 'lavfi', '-i', `${picture}:r=2`, '-t', '180', '-vf', `format=yuv420p${label}`, '-c:v', 'libvpx', '-deadline', 'realtime', '-cpu-used', '8', '-b:v', '120k', out])
      : run(['-f', 'lavfi', '-i', `${picture}:r=2`, '-t', '180', '-vf', `format=yuv420p${label}`, '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-f', 'mp4', out]);
    return made ? out : null;
  }
  if (run(['-f', 'lavfi', '-i', picture, '-vf', `format=rgb24${label}`, '-frames:v', '1', '-update', '1', out])) return out;
  // A format this ffmpeg cannot write: a PNG under that name still decodes as a picture.
  return run(['-f', 'lavfi', '-i', picture, '-vf', `format=rgb24${label}`, '-frames:v', '1', '-update', '1', '-f', 'image2', '-c:v', 'png', out]) ? out : null;
}

function stagePublic(resolved, edit) {
  const pub = path.join(work, 'public');
  fs.rmSync(pub, {recursive: true, force: true});
  fs.mkdirSync(pub, {recursive: true});
  const wanted = new Set();
  const inProps = new Set();
  const add = (rel) => rel && wanted.add(rel);
  const clip = (c) => {
    add(c.src);
    const l = c.layout;
    if (!l) return;
    if (l.slots) l.slots.forEach(clip);
    for (const key of ['base', 'inset', 'media', 'backdrop']) if (l[key]) clip(l[key]);
  };
  resolved.main.forEach(clip);
  add(resolved.audio.mix);
  const prop = (rel) => (add(rel), inProps.add(rel));
  edit.main.forEach((item) => clipSources(item, (c) => propPaths(c.props).forEach(prop)));
  (edit.overlays ?? []).forEach((ov) => propPaths(ov.props).forEach(prop));
  const absent = new Set(resolved.pending);
  for (const rel of wanted) {
    const src = path.join(project, rel);
    const dst = path.join(pub, rel);
    // What the check counted as still generating is not served, even if a file is there by
    // now: the timeline was resolved without it.
    if (fs.existsSync(src) && !absent.has(rel)) place(fs.realpathSync(src), dst);
    else if (draft && inProps.has(rel)) {
      const stand = standIn(rel, resolved);
      if (stand) place(stand, dst);
    }
  }
  return pub;
}

/** What went wrong in the renderer, in a few lines the director can act on. */
function explain(err) {
  const text = String(err?.message ?? err);
  const lines = text.split('\n').map((line) => line.trim()).filter((line) => line && !line.startsWith('at ') && line !== '---');
  const hint = /could not be found|status code of 404|Failed to load resource/.test(text)
    ? ' A scene asked for a file that is not in the project: name media in props by their project-relative path (assets/..., refs/...), so they are staged and checked like any other file.'
    : '';
  return `${lines.slice(0, 5).join(' ').slice(0, 900)}${hint}`;
}

async function openStage(resolved, edit) {
  const {bundle} = await import('@remotion/bundler');
  const {openBrowser, selectComposition} = await import('@remotion/renderer');
  const entry = path.join(comp, 'src', 'index.ts');
  if (!fs.existsSync(entry)) fail('composition/src/index.ts is missing. Run `kit init` to stage the workspace.');
  const publicDir = stagePublic(resolved, edit);
  const outDir = path.join(work, 'bundle');
  fs.rmSync(outDir, {recursive: true, force: true});
  let t = Date.now();
  const serveUrl = await bundle({
    entryPoint: entry,
    rootDir: comp,
    publicDir,
    outDir,
    symlinkPublicDir: true,
    // Remotion keeps its build cache under node_modules, which a job only links to (read-only).
    enableCaching: false,
    onSymlinkDetected: () => {},
    webpackOverride: (config) => ({
      ...config,
      resolve: {...config.resolve, alias: {...(config.resolve?.alias ?? {}), '@kit': KIT_DIR}},
    }),
  });
  lap('bundle', t);
  t = Date.now();
  const logs = [];
  const onBrowserLog = (log) => {
    const text = String(log.text ?? '');
    if (text.startsWith('KITDIAG ')) {
      try {
        logs.push(JSON.parse(text.slice(8)));
      } catch {}
    } else if (log.type === 'error') logs.push({error: 'browser', text: text.slice(0, 400)});
  };
  // Remotion looks for its browser under node_modules of the working directory.
  process.chdir(comp);
  // Chrome takes its temp folder from the environment it is started with (engine/browserTemp.ts).
  browserTemp = chromeTempDir({platform: process.platform, env: process.env, statfs: (dir) => fs.statfsSync(dir), mkdtemp: (prefix) => fs.mkdtempSync(prefix)});
  browserTempKind = browserTemp ? 'memory' : 'disk';
  let browser;
  try {
    browser = await withTempDir(process.env, browserTemp, () => openBrowser('chrome'));
  } catch (err) {
    dropBrowserTemp();
    throw err;
  }
  const inputProps = {resolved};
  let composition;
  try {
    composition = await selectComposition({serveUrl, id: 'Video', inputProps, puppeteerInstance: browser, onBrowserLog, logLevel: 'error'});
  } catch (err) {
    await closeBrowser(browser);
    throw err;
  }
  lap('open', t);
  return {serveUrl, browser, composition, inputProps, logs, onBrowserLog};
}

/** The renderer echoes every console line of the page, whatever log level it is given. The
 *  layout probe's lines are data for this command (they arrive through onBrowserLog), not
 *  something for its reader, so they are kept out of the output while `run` is at work. */
async function withoutProbeEcho(run) {
  const real = console.log;
  console.log = (...args) => {
    if (!args.some((arg) => typeof arg === 'string' && arg.includes('KITDIAG'))) real(...args);
  };
  try {
    return await run();
  } finally {
    console.log = real;
  }
}

/** A browser left open would outlive a failed command. */
async function closeBrowser(browser) {
  try {
    await browser.close({silent: true});
  } catch {}
  dropBrowserTemp();
}

/** Chrome's own temp folder goes with the browser (it is memory while it stays). */
function dropBrowserTemp() {
  if (!browserTemp) return;
  try {
    // Chrome may still be putting its profile away for a moment after it was told to close.
    fs.rmSync(browserTemp, {recursive: true, force: true, maxRetries: 5, retryDelay: 100});
  } catch {}
  browserTemp = null;
}

function pickFrames(resolved) {
  const last = resolved.durationInFrames - 1;
  const picks = [];
  const add = (frame, weight) => picks.push({frame: Math.max(0, Math.min(last, Math.round(frame))), weight});
  for (const o of resolved.overlays) add(o.from + Math.min(Math.round(resolved.fps * 0.7), (o.to - o.from) * 0.6), 3);
  for (const m of resolved.main) {
    const len = m.to - m.from;
    add(m.from + Math.min(Math.max(m.enter.frames + 3, len * 0.55), len - 1), 2);
  }
  const longest = [...resolved.captions.groups].sort((a, b) => b.words.map((w) => w.text).join(' ').length - a.words.map((w) => w.text).join(' ').length)[0];
  if (longest) add(longest.from + Math.min(5, longest.to - longest.from - 1), 3);
  add(0, 4); // the first frame has to work muted
  picks.sort((a, b) => b.weight - a.weight);
  const chosen = [];
  for (const p of picks) {
    if (chosen.length >= SHEET_FRAMES * 2) break;
    if (chosen.every((c) => Math.abs(c - p.frame) > 6)) chosen.push(p.frame);
  }
  return chosen.sort((a, b) => a - b);
}

function layoutFindings(logs, resolved, reported = []) {
  const diags = [];
  const seen = new Set();
  const once = (key, d) => !seen.has(key) && (seen.add(key), diags.push(d));
  const {width: W, height: H, safe: margin} = resolved;
  // What the app covers: a strip at the top, the caption block at the bottom, and its button
  // column low on the right. The upper part of the frame is free edge to edge.
  const edge = Math.round(W * 0.028);
  const column = {x: W - margin.right, y: Math.round(H * 0.45), w: margin.right, h: H - margin.bottom - Math.round(H * 0.45)};
  const span = new Map(resolved.overlays.map((o) => [o.id, o]));
  // An overlay is judged where it rests. Looks fly things in and out through the edges of the
  // frame on purpose, so the first and last moments of its time on screen are not measured.
  const fly = Math.round(resolved.fps * 0.4);
  // A scene the check already named (not written yet in a draft) needs no second finding.
  const named = new Set(reported.filter((d) => d.code === 'scene-unknown' || d.code === 'scene-unwritten').map((d) => /"([^"]+)"/.exec(d.message)?.[1]));
  for (const log of logs) {
    if (log.error === 'scene-unknown') {
      if (!named.has(log.scene)) once(`s${log.scene}`, {level: 'error', code: 'scene-unknown', message: `Scene "${log.scene}" is not registered (known: ${log.known.join(', ')}).`});
    } else if (log.error === 'card-unknown') once(`c${log.card}`, {level: 'error', code: 'card-unknown', message: `This look has no "${log.card}" card (it has: ${log.known.join(', ')}).`});
    else if (log.error === 'browser') once(`b${log.text}`, {level: 'warning', code: 'browser-error', message: log.text});
    if (!log.overlays) continue;
    const at = `${(log.frame / resolved.fps).toFixed(2)} s`;
    for (const o of log.overlays) {
      if (!o.rect || o.zone === 'full') continue;
      const meant = span.get(o.id);
      if (meant) {
        const settle = Math.min(fly, Math.floor((meant.to - meant.from) / 3));
        if (log.frame < meant.from + settle || log.frame >= meant.to - settle) continue;
      }
      const r = o.rect;
      const slack = Math.round(H * 0.012);
      const where = r.y < margin.top - slack ? `the top ${margin.top} px the app covers`
        : r.y + r.h > H - margin.bottom + slack ? `the bottom ${margin.bottom} px the app covers`
        : r.x < edge || r.x + r.w > W - edge ? 'the edge of the frame'
        : overlaps(r, column) ? "the app's button column on the right"
        : null;
      if (where) once(`safe${o.id}`, {level: 'warning', code: 'overlay-outside-safe-area', message: `Overlay ${o.id} reaches into ${where} at ${at} (it spans x ${r.x}-${r.x + r.w}, y ${r.y}-${r.y + r.h}).`, where: o.id});
      if (log.captions && overlaps(r, log.captions)) once(`cap${o.id}`, {level: 'warning', code: 'overlay-covers-captions', message: `Overlay ${o.id} overlaps the captions at ${at}.`, where: o.id});
    }
  }
  return diags;
}

async function sheet(prepared) {
  const {renderStill} = await import('@remotion/renderer');
  const resolved = {...prepared.resolved, debug: true};
  const stage = await openStage(resolved, prepared.edit);
  const custom = option('frames');
  const frames = custom ? custom.split(',').map((f) => Number(f.trim())).filter((f) => Number.isInteger(f) && f >= 0 && f < resolved.durationInFrames) : pickFrames(resolved);
  const stills = path.join(work, 'stills');
  fs.rmSync(stills, {recursive: true, force: true});
  fs.mkdirSync(stills, {recursive: true});
  let t = Date.now();
  try {
    await Promise.all(frames.map((frame) => renderStill({
      composition: stage.composition, serveUrl: stage.serveUrl, frame, inputProps: stage.inputProps,
      output: path.join(stills, `f${String(frame).padStart(5, '0')}.jpeg`), imageFormat: 'jpeg', jpegQuality: 82, scale: 0.33,
      puppeteerInstance: stage.browser, onBrowserLog: stage.onBrowserLog, logLevel: 'error',
    })));
  } finally {
    await closeBrowser(stage.browser);
  }
  lap(`${frames.length} stills`, t);
  const files = fs.readdirSync(stills).sort().map((f) => path.join(stills, f));
  const sheets = [];
  for (let i = 0; i < files.length; i += SHEET_FRAMES) {
    const group = files.slice(i, i + SHEET_FRAMES);
    const out = path.join(work, sheets.length ? `sheet${sheets.length + 1}.jpg` : 'sheet.jpg');
    const cols = Math.min(SHEET_COLUMNS, group.length);
    const rows = Math.ceil(group.length / cols);
    const args = ['-y', '-v', 'error', ...group.flatMap((f) => ['-i', f]), '-filter_complex', `${group.map((_, k) => `[${k}:v]`).join('')}concat=n=${group.length}:v=1:a=0,tile=${cols}x${rows}:margin=6:padding=6:color=0x101010`, '-frames:v', '1', '-q:v', '3', out];
    const r = spawnSync('ffmpeg', args, {encoding: 'utf8'});
    if (r.status !== 0) fail(`could not build the sheet: ${(r.stderr || '').trim().split('\n').pop()}`);
    sheets.push(out);
  }
  return {sheets, frames, findings: layoutFindings(stage.logs, resolved, prepared.diags)};
}

function loudness(file) {
  const r = spawnSync('ffmpeg', ['-hide_banner', '-nostats', '-i', file, '-af', 'loudnorm=print_format=json', '-f', 'null', '-'], {encoding: 'utf8'});
  const m = /\{[^{}]*"input_i"[^{}]*\}/s.exec(r.stderr || '');
  if (!m) return null;
  try {
    return JSON.parse(m[0]);
  } catch {
    return null;
  }
}

/** The loudness of a file's sound over time: one value a millisecond (RMS over 10 ms), for the
 *  first `seconds`. Enough to place one sound against another to about a millisecond. */
function loudnessCurve(file, seconds) {
  const r = spawnSync('ffmpeg', ['-v', 'error', '-t', String(seconds), '-i', file, '-vn', '-ac', '1', '-ar', '8000', '-f', 's16le', '-'], {maxBuffer: 256 * 1024 * 1024});
  if (r.status !== 0 || !r.stdout || r.stdout.length < 16000) return null;
  const bytes = r.stdout;
  const pcm = new Int16Array(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.length - (bytes.length % 2)));
  const hop = 8;
  const win = 80;
  const n = Math.floor((pcm.length - win) / hop);
  if (n < 1000) return null;
  const out = new Float32Array(n);
  let sum = 0;
  for (let i = 0; i < win; i++) sum += pcm[i] * pcm[i];
  for (let k = 0; k < n; k++) {
    out[k] = Math.sqrt(sum / win);
    for (let i = 0; i < hop; i++) {
      const gone = pcm[k * hop + i];
      const come = pcm[k * hop + win + i];
      sum += come * come - gone * gone;
    }
  }
  return out;
}

/** How far the sound of `rendered` runs behind the soundtrack it was made from, in seconds
 *  (positive: the export's sound is late against its picture), with how well the two agree.
 *  null when it cannot be told (no sound, silence, or the two do not match).
 *
 *  Why this exists: the renderer writes its audio with the encoder's lead-in still in front
 *  (two AAC frames, 42.7 ms at 48 kHz) and nothing in the file that tells a player to skip it,
 *  so every export came out with its sound that much behind the lips (measured on video 713:
 *  0.0428 s, the same in the raw render). */
function soundLag(rendered, soundtrack) {
  const want = loudnessCurve(soundtrack, 30);
  const got = loudnessCurve(rendered, 30.5);
  if (!want || !got) return null;
  const n = Math.min(want.length, got.length) - 300;
  if (n < 1000) return null;
  let best = {agreement: -2, ms: 0};
  for (let lag = -100; lag <= 250; lag++) {
    let sa = 0, sb = 0, saa = 0, sbb = 0, sab = 0, count = 0;
    for (let k = 100; k < n; k += 2) {
      const a = want[k];
      const b = got[k + lag];
      sa += a; sb += b; saa += a * a; sbb += b * b; sab += a * b; count++;
    }
    const cov = sab / count - (sa / count) * (sb / count);
    const va = saa / count - (sa / count) ** 2;
    const vb = sbb / count - (sb / count) ** 2;
    const agreement = va > 0 && vb > 0 ? cov / Math.sqrt(va * vb) : -2;
    if (agreement > best.agreement) best = {agreement, ms: lag};
  }
  return best.agreement >= 0.8 ? {seconds: best.ms / 1000, agreement: +best.agreement.toFixed(3)} : null;
}

/** For the export line: where the finished file's sound sits against its picture. A fact for the
 *  record, not a finding: nothing in the edit can move it. */
function soundPlacement(offsets) {
  if (!offsets || offsets.exported === null) return 'sound not checked against the picture';
  if (Math.abs(offsets.exported) > 15) return `sound ${Math.abs(offsets.exported)} ms ${offsets.exported > 0 ? 'behind' : 'ahead of'} the picture`;
  return 'sound on the picture';
}

async function render(prepared) {
  const {renderMedia} = await import('@remotion/renderer');
  const resolved = prepared.resolved;
  const stage = await openStage(resolved, prepared.edit);
  const renders = path.join(project, 'renders');
  const exportsDir = path.join(project, 'exports');
  fs.mkdirSync(renders, {recursive: true});
  fs.mkdirSync(exportsDir, {recursive: true});
  const name = (option('name', 'video') || 'video').replace(/[^A-Za-z0-9_.-]+/g, '_').replace(/\.mp4$/i, '');
  const raw = path.join(renders, `${name}_raw.mp4`);
  const out = path.join(exportsDir, `${name}.mp4`);
  let t = Date.now();
  try {
    await renderMedia({
      composition: stage.composition, serveUrl: stage.serveUrl, inputProps: stage.inputProps, codec: 'h264', outputLocation: raw,
      crf: 18, pixelFormat: 'yuv420p', colorSpace: 'bt709', puppeteerInstance: stage.browser, onBrowserLog: stage.onBrowserLog, logLevel: 'error',
    });
  } finally {
    await closeBrowser(stage.browser);
  }
  lap('render', t);
  t = Date.now();
  const hasAudio = Boolean(resolved.audio.mix);
  const soundtrack = hasAudio ? path.join(project, resolved.audio.mix) : null;
  const tag = ['-bsf:v', 'h264_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1:video_full_range_flag=0', '-movflags', '+faststart'];
  let lufs = null;
  let corrected = false;
  const measured = hasAudio ? loudness(raw) : null;
  // Sound against picture: the renderer's output carries its sound late (see soundLag). The
  // lead-in is cut off by exactly what is measured, in the one audio pass this step makes anyway.
  // Only a lag an encoder's lead-in can explain is cut (a few AAC frames, well under 0.1 s).
  const lag = hasAudio ? soundLag(raw, soundtrack) : null;
  const cut = lag && lag.seconds > 0.004 && lag.seconds <= 0.1 ? lag.seconds : 0;
  const filters = [];
  if (cut) filters.push(`atrim=start=${cut.toFixed(4)}`, 'asetpts=PTS-STARTPTS');
  if (measured && Math.abs(Number(measured.input_i) - TARGET_LUFS) > 1) {
    filters.push(`loudnorm=I=${TARGET_LUFS}:TP=-1.5:LRA=11:measured_I=${measured.input_i}:measured_TP=${measured.input_tp}:measured_LRA=${measured.input_lra}:measured_thresh=${measured.input_thresh}:offset=${measured.target_offset}:linear=true`);
    corrected = true;
  }
  if (filters.length) {
    // One audio-only pass; the picture is copied untouched. Every export with sound goes through
    // it now, so it keeps a high bitrate: this is the sound's second encode.
    const r = spawnSync('ffmpeg', ['-y', '-v', 'error', '-i', raw, '-c:v', 'copy', ...tag, '-af', filters.join(','), '-c:a', 'aac', '-b:a', '256k', '-ar', '48000', out], {encoding: 'utf8'});
    if (r.status !== 0) fail(`could not finish the export's sound: ${(r.stderr || '').trim().split('\n').pop()}`);
    lufs = corrected ? Number(loudness(out)?.input_i ?? NaN) : measured ? Number(measured.input_i) : null;
  } else {
    const r = spawnSync('ffmpeg', ['-y', '-v', 'error', '-i', raw, '-c', 'copy', ...tag, out], {encoding: 'utf8'});
    if (r.status !== 0) fail(`could not finish the export: ${(r.stderr || '').trim().split('\n').pop()}`);
    lufs = measured ? Number(measured.input_i) : null;
  }
  // Measured again on the finished file: this is the number that says sound and picture agree.
  const after = hasAudio ? soundLag(out, soundtrack) : null;
  const probe = spawnSync('ffprobe', ['-v', 'error', '-show_entries', 'stream=codec_type,codec_name,width,height,pix_fmt,color_range,color_space,r_frame_rate:format=duration,size', '-of', 'json', out], {encoding: 'utf8'});
  const info = JSON.parse(probe.stdout || '{}');
  const v = (info.streams ?? []).find((s) => s.codec_type === 'video') ?? {};
  const a = (info.streams ?? []).find((s) => s.codec_type === 'audio');
  const expected = resolved.durationInFrames / resolved.fps;
  const problems = [];
  if (v.codec_name !== 'h264') problems.push(`video codec is ${v.codec_name}, expected h264`);
  if (v.width !== resolved.width || v.height !== resolved.height) problems.push(`size is ${v.width}x${v.height}, expected ${resolved.width}x${resolved.height}`);
  if (v.pix_fmt !== 'yuv420p') problems.push(`pixel format is ${v.pix_fmt}`);
  if (v.color_range !== 'tv' || v.color_space !== 'bt709') problems.push(`colour is ${v.color_range}/${v.color_space}, expected tv/bt709`);
  if (Math.abs(Number(info.format?.duration ?? 0) - expected) > 0.15) problems.push(`duration is ${info.format?.duration} s, expected ${expected.toFixed(2)} s`);
  if (hasAudio && !a) problems.push('the export has no sound although the edit has a soundtrack');
  lap('finish', t);
  const result = {
    file: path.relative(project, out), seconds: +Number(info.format?.duration ?? 0).toFixed(3), bytes: Number(info.format?.size ?? 0),
    video: `${v.codec_name} ${v.width}x${v.height} ${v.pix_fmt} ${v.color_range}/${v.color_space}`, audio: a ? a.codec_name : null,
    lufs, loudness_corrected: corrected,
    // Sound against picture, in milliseconds (positive: the sound is late): as the renderer
    // left it, and in the finished file.
    sound_offset_ms: hasAudio ? {rendered: lag ? Math.round(lag.seconds * 1000) : null, exported: after ? Math.round(after.seconds * 1000) : null} : null,
    // Where Chrome kept its temporary files: 'memory', or 'disk' when it could not (a render
    // on disk can run several times longer, see engine/browserTemp.ts).
    chrome_temp: browserTempKind,
    problems, findings: layoutFindings(stage.logs, resolved, prepared.diags),
  };
  fs.writeFileSync(path.join(renders, 'kit_render.json'), JSON.stringify(result, null, 1));
  return result;
}

// ---------------------------------------------------------------- commands

if (!['check', 'sheet', 'render', 'resolve'].includes(command)) {
  fail('usage: kit <check|sheet|render|resolve> [--project DIR] [--draft] [--frames 10,200] [--name NAME] [--json]');
}
const prepared = await prepare();

if (command === 'resolve') {
  if (!prepared.resolved) {
    report(prepared.diags, null);
    process.exit(1);
  }
  fs.mkdirSync(work, {recursive: true});
  fs.writeFileSync(path.join(work, 'resolved.json'), JSON.stringify(prepared.resolved, null, 1));
  console.log(`resolved timeline: ${path.relative(project, path.join(work, 'resolved.json'))}`);
  process.exit(prepared.diags.some((d) => d.level === 'error') ? 1 : 0);
}

if (command === 'check') {
  const summary = report(prepared.diags, prepared.resolved);
  process.exit(summary.ok ? 0 : 1);
}

if (!prepared.resolved || prepared.diags.some((d) => d.level === 'error')) {
  report(prepared.diags, prepared.resolved);
  console.log(command === 'render' ? 'Nothing was rendered: fix the errors above first.' : 'No sheet was made: fix the errors above first.');
  process.exit(1);
}

if (command === 'sheet') {
  let made;
  try {
    made = await withoutProbeEcho(() => sheet(prepared));
  } catch (err) {
    fail(`the sheet could not be made. ${explain(err)}`);
  }
  const diags = [...prepared.diags, ...made.findings];
  const summary = report(diags, prepared.resolved, {sheets: made.sheets.map((s) => path.relative(project, s)), frames: made.frames});
  console.log(`Sheet${made.sheets.length > 1 ? 's' : ''}: ${made.sheets.map((s) => path.relative(project, s)).join(', ')} (${made.frames.length} frames, ${((Date.now() - started) / 1000).toFixed(1)} s). Look at ${made.sheets.length > 1 ? 'them' : 'it'} before rendering.`);
  process.exit(summary.ok ? 0 : 1);
}

if (prepared.resolved.pending.length) {
  report(prepared.diags, prepared.resolved);
  console.log('Nothing was rendered: the final render needs every file in place.');
  process.exit(1);
}
let result;
try {
  result = await withoutProbeEcho(() => render(prepared));
} catch (err) {
  fail(`the video could not be rendered. ${explain(err)}`);
}
report([...prepared.diags, ...result.findings], prepared.resolved, {export: result});
if (result.problems.length) {
  console.log(`EXPORT PROBLEMS: ${result.problems.join('; ')}`);
  process.exit(1);
}
console.log(`Exported ${result.file}: ${result.seconds} s, ${result.video}${result.audio ? `, ${result.audio}, ${soundPlacement(result.sound_offset_ms)}` : ', no sound'}${result.lufs !== null ? `, ${result.lufs.toFixed(1)} LUFS${result.loudness_corrected ? ' (corrected)' : ''}` : ''} (${((Date.now() - started) / 1000).toFixed(1)} s).`);
process.exit(0);
