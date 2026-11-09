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
import {lintResolved} from '../engine/lint.ts';
import {estimateVoice, resolveEdit} from '../engine/resolve.ts';
import {validateEdit} from '../engine/schema.ts';
import {BUILTIN_SCENES} from '../engine/types.ts';
import {overlaps} from '../engine/zones.ts';

const KIT_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const MEDIA_RE = /\.(png|jpe?g|webp|gif|svg|mp4|mov|webm|m4v|mp3|wav|m4a|aac|ogg)$/i;
const VIDEO_RE = /\.(mp4|mov|webm|m4v)$/i;
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

/** Everything `check` knows: the resolved timeline and every finding. */
async function prepare() {
  const editFile = path.join(comp, 'edit.json');
  const edit = readJson(editFile, 'The edit');
  if (edit === null) fail(`no edit found at ${path.relative(process.cwd(), editFile) || editFile}. Write composition/edit.json first.`);
  const diags = validateEdit(edit);
  if (diags.some((d) => d.level === 'error')) return {edit, diags, resolved: null, meta: {}};

  const voiceFile = path.join(work, 'voice.json');
  const voice = readJson(voiceFile, 'The voice timings');
  const meta = await lookMeta(diags);

  // Facts about every file the edit can reach.
  const media = {};
  const note = (rel) => {
    if (!rel || media[rel]) return;
    const abs = path.join(project, rel);
    const exists = fs.existsSync(abs);
    media[rel] = {exists, duration: exists && VIDEO_RE.test(rel) ? probeDuration(abs) : undefined};
  };
  const lines = (voice ?? estimateVoice(edit)).lines ?? [];
  for (const line of lines) for (const part of line.parts) if (part.take) note(part.take);
  for (const item of edit.main) clipSources(item, (c) => [c.video, c.image, MEDIA_RE.test(c.freeze ?? '') ? c.freeze : null].forEach(note));
  if (edit.audio?.mix) note(edit.audio.mix);
  // A soundtrack made by `kit mix` is used without being named.
  if (!edit.audio?.mix) {
    const made = ['assets/audio/mix.wav', 'assets/audio/mix.mp3'].find((rel) => fs.existsSync(path.join(project, rel)));
    if (made) edit.audio = {...(edit.audio ?? {}), mix: made};
  }
  if (edit.audio?.mix) note(edit.audio.mix);

  const resolved = resolveEdit({edit, voice, media, draft, captionRules: meta.captions});
  diags.push(...resolved.diagnostics);

  // Files named in props, and scene names.
  const known = new Set([...BUILTIN_SCENES, ...customScenes()]);
  const pending = new Set(resolved.pending);
  const checkProps = (props, where) => {
    for (const rel of propPaths(props)) {
      if (fs.existsSync(path.join(project, rel))) continue;
      if (!pending.has(rel) && !draft) diags.push({level: 'error', code: 'asset-missing', message: `${rel} does not exist.`, where});
      pending.add(rel);
    }
  };
  edit.main.forEach((item, i) => clipSources(item, (c) => {
    if (c.scene === undefined) return;
    checkProps(c.props, `main[${i}]`);
    if (!known.has(c.scene)) diags.push({level: 'error', code: 'scene-unknown', message: `Scene "${c.scene}" is neither a ready-made scene (${BUILTIN_SCENES.join(', ')}) nor registered in composition/src/scenes.ts.`, where: `main[${i}]`});
  }));
  (edit.overlays ?? []).forEach((ov, i) => {
    checkProps(ov.props, `overlays[${i}]`);
    if (ov.card !== undefined && meta.cardZones && !(ov.card in meta.cardZones)) {
      diags.push({level: 'warning', code: 'card-unknown', message: `Card "${ov.card}" is not listed by this look (it has: ${Object.keys(meta.cardZones).join(', ')}). Add it to the look or use one it has.`, where: `overlays[${i}]`});
    }
    if (ov.scene !== undefined && !known.has(ov.scene)) diags.push({level: 'error', code: 'scene-unknown', message: `Scene "${ov.scene}" is not registered in composition/src/scenes.ts.`, where: `overlays[${i}]`});
  });
  resolved.pending = [...pending].sort();
  diags.push(...lintResolved(resolved, edit, meta));
  return {edit, diags, resolved, meta};
}

// ---------------------------------------------------------------- reporting

function report(diags, resolved, extra = {}) {
  const count = (level) => diags.filter((d) => d.level === level).length;
  const summary = {
    ok: count('error') === 0,
    errors: count('error'),
    warnings: count('warning'),
    seconds: resolved ? +(resolved.durationInFrames / resolved.fps).toFixed(2) : null,
    shots: resolved ? resolved.main.length : null,
    overlays: resolved ? resolved.overlays.length : null,
    captions: resolved ? resolved.captions.groups.length : null,
    voice: resolved ? (resolved.voiceEstimated ? 'estimated from the script' : 'timed') : null,
    pending: resolved ? resolved.pending : [],
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
    console.log(`${summary.ok ? 'OK' : 'NOT OK'}: ${summary.seconds} s, ${summary.shots} shots, ${summary.overlays} overlays, ${summary.captions} caption phrases, voice ${summary.voice}; ${summary.errors} error(s), ${summary.warnings} warning(s).`);
    if (summary.pending.length) console.log(`${draft ? 'Still generating' : 'MISSING'} (${summary.pending.length}): ${summary.pending.join(', ')}`);
  } else console.log(`NOT OK: the edit has ${summary.errors} error(s); fix them and run the check again.`);
  return summary;
}

// ---------------------------------------------------------------- rendering

function stagePublic(resolved, edit) {
  const pub = path.join(work, 'public');
  fs.rmSync(pub, {recursive: true, force: true});
  fs.mkdirSync(pub, {recursive: true});
  const wanted = new Set();
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
  edit.main.forEach((item) => clipSources(item, (c) => propPaths(c.props).forEach(add)));
  (edit.overlays ?? []).forEach((ov) => propPaths(ov.props).forEach(add));
  for (const rel of wanted) {
    const src = path.join(project, rel);
    if (!fs.existsSync(src)) continue;
    const dst = path.join(pub, rel);
    fs.mkdirSync(path.dirname(dst), {recursive: true});
    // Remotion's file server refuses symlinked files, so: a hard link (instant, no bytes
    // copied) and, across filesystems, a copy.
    const real = fs.realpathSync(src);
    try {
      fs.linkSync(real, dst);
    } catch {
      fs.copyFileSync(real, dst);
    }
  }
  return pub;
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
  const browser = await openBrowser('chrome');
  const inputProps = {resolved};
  const composition = await selectComposition({serveUrl, id: 'Video', inputProps, puppeteerInstance: browser, onBrowserLog});
  lap('open', t);
  return {serveUrl, browser, composition, inputProps, logs, onBrowserLog};
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

function layoutFindings(logs, resolved) {
  const diags = [];
  const seen = new Set();
  const once = (key, d) => !seen.has(key) && (seen.add(key), diags.push(d));
  const {width: W, height: H, safe: margin} = resolved;
  // What the app covers: a strip at the top, the caption block at the bottom, and its button
  // column low on the right. The upper part of the frame is free edge to edge.
  const edge = Math.round(W * 0.028);
  const column = {x: W - margin.right, y: Math.round(H * 0.45), w: margin.right, h: H - margin.bottom - Math.round(H * 0.45)};
  for (const log of logs) {
    if (log.error === 'scene-unknown') once(`s${log.scene}`, {level: 'error', code: 'scene-unknown', message: `Scene "${log.scene}" is not registered (known: ${log.known.join(', ')}).`});
    else if (log.error === 'card-unknown') once(`c${log.card}`, {level: 'error', code: 'card-unknown', message: `This look has no "${log.card}" card (it has: ${log.known.join(', ')}).`});
    else if (log.error === 'browser') once(`b${log.text}`, {level: 'warning', code: 'browser-error', message: log.text});
    if (!log.overlays) continue;
    const at = `${(log.frame / resolved.fps).toFixed(2)} s`;
    for (const o of log.overlays) {
      if (!o.rect || o.zone === 'full') continue;
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
  await Promise.all(frames.map((frame) => renderStill({
    composition: stage.composition, serveUrl: stage.serveUrl, frame, inputProps: stage.inputProps,
    output: path.join(stills, `f${String(frame).padStart(5, '0')}.jpeg`), imageFormat: 'jpeg', jpegQuality: 82, scale: 0.33,
    puppeteerInstance: stage.browser, onBrowserLog: stage.onBrowserLog,
  })));
  await stage.browser.close({silent: true});
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
  return {sheets, frames, findings: layoutFindings(stage.logs, resolved)};
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
  await renderMedia({
    composition: stage.composition, serveUrl: stage.serveUrl, inputProps: stage.inputProps, codec: 'h264', outputLocation: raw,
    crf: 18, pixelFormat: 'yuv420p', colorSpace: 'bt709', puppeteerInstance: stage.browser, onBrowserLog: stage.onBrowserLog, logLevel: 'error',
  });
  await stage.browser.close({silent: true});
  lap('render', t);
  t = Date.now();
  const hasAudio = Boolean(resolved.audio.mix);
  const tag = ['-bsf:v', 'h264_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1:video_full_range_flag=0', '-movflags', '+faststart'];
  let lufs = null;
  let corrected = false;
  const measured = hasAudio ? loudness(raw) : null;
  if (measured && Math.abs(Number(measured.input_i) - TARGET_LUFS) > 1) {
    // Off target: one audio-only pass, the picture is copied untouched.
    const af = `loudnorm=I=${TARGET_LUFS}:TP=-1.5:LRA=11:measured_I=${measured.input_i}:measured_TP=${measured.input_tp}:measured_LRA=${measured.input_lra}:measured_thresh=${measured.input_thresh}:offset=${measured.target_offset}:linear=true`;
    const r = spawnSync('ffmpeg', ['-y', '-v', 'error', '-i', raw, '-c:v', 'copy', ...tag, '-af', af, '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', out], {encoding: 'utf8'});
    if (r.status !== 0) fail(`loudness correction failed: ${(r.stderr || '').trim().split('\n').pop()}`);
    corrected = true;
    lufs = Number(loudness(out)?.input_i ?? NaN);
  } else {
    const r = spawnSync('ffmpeg', ['-y', '-v', 'error', '-i', raw, '-c', 'copy', ...tag, out], {encoding: 'utf8'});
    if (r.status !== 0) fail(`could not finish the export: ${(r.stderr || '').trim().split('\n').pop()}`);
    lufs = measured ? Number(measured.input_i) : null;
  }
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
    lufs, loudness_corrected: corrected, problems, findings: layoutFindings(stage.logs, resolved),
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
  const made = await sheet(prepared);
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
const result = await render(prepared);
report([...prepared.diags, ...result.findings], prepared.resolved, {export: result});
if (result.problems.length) {
  console.log(`EXPORT PROBLEMS: ${result.problems.join('; ')}`);
  process.exit(1);
}
console.log(`Exported ${result.file}: ${result.seconds} s, ${result.video}${result.audio ? `, ${result.audio}` : ', no sound'}${result.lufs !== null ? `, ${result.lufs.toFixed(1)} LUFS${result.loudness_corrected ? ' (corrected)' : ''}` : ''} (${((Date.now() - started) / 1000).toFixed(1)} s).`);
process.exit(0);
