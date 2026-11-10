// The engine's pure logic: run with `node --test remotion-composer/kit/test/`.

import assert from 'node:assert/strict';
import test from 'node:test';
import {chromeTempDir, MEMORY_DIR, ROOM_BYTES, TMPFS_MAGIC, withTempDir} from '../engine/browserTemp.ts';
import {groupWords, takePhrases} from '../engine/captions.ts';
import {contrastRatio} from '../engine/color.ts';
import {lintResolved} from '../engine/lint.ts';
import {estimateVoice, resolveEdit} from '../engine/resolve.ts';
import {validateEdit} from '../engine/schema.ts';
import type {Edit, Voice} from '../engine/types.ts';
import {findPhrase, isRtl, isUnspaced, norm, tokenize} from '../engine/words.ts';
import {zonesFor} from '../engine/zones.ts';

/** Three lines; line B was too long for one take and is spoken in two parts. Six words a second. */
function voice(): Voice {
  const part = (id: string, start: number, text: string) => {
    const tokens = text.split(' ');
    return {id, start, dur: tokens.length * 0.5, take: `assets/video/take_${id}.mp4`, words: tokens.map((t, i) => ({text: t, start: start + i * 0.5, end: start + i * 0.5 + 0.4}))};
  };
  return {
    lines: [
      {id: 'A', parts: [part('A', 0, 'Be honest: your pictures look fake.')]},
      {id: 'B', parts: [part('B_1', 3, 'Upload any picture. Generative detail rebuilds it up to four times bigger.'), part('B_2', 9, 'Faces, fur, food, interiors. One click.')]},
      {id: 'C', parts: [part('C', 12, 'Stop posting fakes. Try it free.')]},
    ],
  };
}

const edit = (over: Partial<Edit> = {}): Edit => ({
  canvas: {size: '1080x1920', fps: 30, safe: 'tiktok'},
  voice: {lines: [
    {id: 'A', take: true, text: 'Be honest: your pictures look fake.'},
    {id: 'B', take: true, text: 'Upload any picture. Generative detail rebuilds it up to four times bigger. Faces, fur, food, interiors. One click.'},
    {id: 'C', take: true, text: 'Stop posting fakes. Try it free.'},
  ]},
  main: [
    {take: 'A'},
    {take: 'B', until: {word: 'Generative'}, enter: 'whip'},
    {video: 'assets/video/broll.mp4', until: {word: 'Faces'}, enter: {type: 'punch', frames: 5}},
    {scene: 'BeforeAfter', props: {pair: 'refs/pet'}, until: {word: 'One'}, enter: 'slide-up'},
    {take: 'B', until: {line: 'C'}, enter: 'punch'},
    {take: 'C'},
    {freeze: 'C', for: 2},
  ],
  overlays: [
    {tag: 'up to 4x', from: {word: 'four'}, until: {word: 'Faces'}},
    {card: 'cta', props: {title: 'Try it'}, from: {word: 'Stop'}, until: {voice: 'end'}},
    {card: 'end', from: {voice: 'end'}},
  ],
  ...over,
});

const errors = (diags: {level: string; code: string}[]) => diags.filter((d) => d.level === 'error').map((d) => d.code);

test('words are found whatever their case, punctuation or hyphens', () => {
  assert.equal(norm('Re-renders,'), 'rerenders');
  const words = voice().lines.flatMap((l) => l.parts.flatMap((p) => p.words.map((w) => ({...w, line: l.id, part: p.id}))));
  assert.equal(findPhrase(words, 'faces')?.[0].start, 9);
  assert.equal(findPhrase(words, 'up to four')?.[0].text, 'up');
  assert.equal(findPhrase(words, 'it', {nth: 2})?.[0].start, 14);
  assert.equal(findPhrase(words, 'it', {after: 10})?.[0].start, 14);
  assert.equal(findPhrase(words, 'nowhere'), null);
});

test('cuts land on the spoken words and the sequence has no gaps', () => {
  const r = resolveEdit({edit: edit(), voice: voice()});
  assert.deepEqual(errors(r.diagnostics), []);
  assert.deepEqual(r.main.map((m) => [m.id, m.from, m.to, m.kind]), [
    ['m1', 0, 90, 'take'],       // line A, whole (3 s)
    ['m2', 90, 135, 'take'],     // line B until "Generative" (4.5 s)
    ['m3', 135, 270, 'video'],   // b-roll until "Faces" (9 s)
    ['m4', 270, 330, 'scene'],   // before/after until "One" (11 s)
    ['m5', 330, 360, 'take'],    // back on the presenter until line C (12 s)
    ['m6', 360, 450, 'take'],    // line C, whole
    ['m7', 450, 510, 'freeze'],  // two seconds on its last frame
  ]);
  for (let i = 1; i < r.main.length; i++) assert.equal(r.main[i].from, r.main[i - 1].to);
  assert.equal(r.durationInFrames, 510);
  assert.deepEqual(lintResolved(r, edit()).filter((d) => d.code === 'coverage-gap'), []);
});

test('a presenter take always shows the frame that matches the voice, also after a cutaway', () => {
  const r = resolveEdit({edit: edit(), voice: voice()});
  const [a, b1, , , b2, c, hold] = r.main;
  assert.equal(a.src, 'assets/video/take_A.mp4');
  assert.equal(a.trimBefore, 0);
  assert.equal(b1.src, 'assets/video/take_B_1.mp4');
  assert.equal(b1.trimBefore, 0);
  // Back on the presenter at 11 s: that is 2 s into the second part of line B.
  assert.equal(b2.src, 'assets/video/take_B_2.mp4');
  assert.equal(b2.trimBefore, 60);
  assert.equal(c.trimBefore, 0);
  assert.equal(hold.kind, 'freeze');
  assert.equal(hold.src, 'assets/video/take_C.mp4');
});

test('a take that runs across two parts of a line changes footage at the part boundary', () => {
  const e = edit({main: [{take: 'A'}, {take: 'B'}, {take: 'C'}], overlays: []});
  const r = resolveEdit({edit: e, voice: voice()});
  assert.deepEqual(r.main.map((m) => [m.id, m.src, m.from, m.to, m.enter.type]), [
    ['m1', 'assets/video/take_A.mp4', 0, 90, 'none'],
    ['m2', 'assets/video/take_B_1.mp4', 90, 270, 'none'],
    ['m2.2', 'assets/video/take_B_2.mp4', 270, 360, 'none'],
    ['m3', 'assets/video/take_C.mp4', 360, 450, 'none'],
  ]);
});

test('the outgoing shot stays alive underneath the incoming entrance', () => {
  const r = resolveEdit({edit: edit(), voice: voice()});
  assert.equal(r.main[0].tail, 8);  // next enters with a 6-frame whip
  assert.equal(r.main[1].tail, 7);  // next enters with a 5-frame punch
  assert.equal(r.main[5].tail, 0);  // the hold just cuts in
  assert.equal(r.main[0].enter.type, 'none');
});

test('an item that would flash by is dropped and the next one takes over from the same moment', () => {
  const e = edit({main: [{take: 'A', until: {word: 'honest'}}, {video: 'x.mp4', until: {word: 'your', offset: -0.45}}, {take: 'A'}, {take: 'B'}, {take: 'C'}], overlays: []});
  const r = resolveEdit({edit: e, voice: voice()});
  assert.ok(r.diagnostics.some((d) => d.code === 'item-dropped-short'));
  assert.equal(r.main[1].src, 'assets/video/take_A.mp4');
  assert.equal(r.main[1].from, r.main[0].to);
});

test('a word that is not in the script is an error that names close words', () => {
  const e = edit();
  e.main[1] = {take: 'B', until: {word: 'Generativ'}};
  const r = resolveEdit({edit: e, voice: voice()});
  const d = r.diagnostics.find((x) => x.code === 'anchor-word-not-found');
  assert.ok(d && /Generative/.test(d.message));
});

test('a word spoken only before the cut is reported as such', () => {
  const e = edit();
  e.main[2] = {video: 'assets/video/broll.mp4', until: {word: 'honest'}};
  const d = resolveEdit({edit: e, voice: voice()}).diagnostics.find((x) => x.code === 'anchor-word-not-found');
  assert.ok(d && /before this point/.test(d.message));
});

test('a word can be pinned to the line it is spoken in', () => {
  // "it" is spoken in line B (6 s) and again in line C (14 s).
  const e = edit({overlays: [{tag: 'in C', from: {word: 'it', line: 'C'}, for: 1}, {tag: 'first', from: {word: 'it'}, for: 1}]});
  assert.deepEqual(validateEdit(e), []);
  const r = resolveEdit({edit: e, voice: voice()});
  assert.deepEqual(errors(r.diagnostics), []);
  assert.deepEqual(r.overlays.map((o) => o.from), [420, 180]);
  // Only the unpinned one is ambiguous.
  assert.deepEqual(r.diagnostics.filter((d) => d.code === 'anchor-ambiguous').map((d) => d.where), ['overlays[1].from']);
  // In the main sequence too, with the end of the word (14.4 s).
  const cut = edit({main: [{take: 'A'}, {take: 'B', until: {word: 'it', line: 'C', edge: 'end'}}, {take: 'C'}], overlays: []});
  assert.deepEqual(validateEdit(cut), []);
  const shown = resolveEdit({edit: cut, voice: voice()}).main.filter((m) => m.id.startsWith('m2'));
  assert.equal(shown[shown.length - 1].to, 432);
  // Two moments at once are still refused, and so is a line that does not exist.
  const bad = (until: unknown) => validateEdit({...edit(), main: [{video: 'a.mp4', until}]}).some((d) => d.code === 'anchor-invalid');
  assert.ok(bad({word: 'it', t: 3}));
  assert.ok(bad({line: 'C', voice: 'end'}));
  assert.ok(bad({word: 'it', line: 'Q'}));
});

test('seconds, line ends and the end of the voice are moments too', () => {
  const e = edit({main: [{color: '#000', until: 1.5}, {take: 'A', until: {line: 'A', edge: 'end'}}, {take: 'B', until: {voice: 'end', offset: -3}}, {take: 'C'}], overlays: [{text: 'hi', from: 0.5, for: 1}, {tag: 'late', from: {end: true, offset: -1}}]});
  const r = resolveEdit({edit: e, voice: voice()});
  assert.deepEqual(errors(r.diagnostics), []);
  assert.deepEqual(r.main.map((m) => m.to), [45, 90, 270, 360, 450]);
  assert.deepEqual(r.overlays.map((o) => [o.kind, o.from, o.to]), [['text', 15, 45], ['tag', 420, 450]]);
});

test('before any voice exists the script gives an estimate, and over-long lines are flagged', () => {
  const r = resolveEdit({edit: edit()});
  assert.equal(r.voiceEstimated, true);
  assert.deepEqual(errors(r.diagnostics), []);
  assert.ok(r.durationInFrames > 300);
  const long = edit();
  long.voice!.lines[1].text = 'word '.repeat(60).trim();
  assert.ok(resolveEdit({edit: long}).diagnostics.some((d) => d.code === 'line-long-for-take'));
  const est = estimateVoice(edit());
  assert.equal(est.lines[0].parts[0].take, 'assets/video/take_A.mp4');
});

test('timings made from an older script are refused', () => {
  const v = voice();
  v.lines[0].text = 'Something else entirely.';
  assert.ok(errors(resolveEdit({edit: edit(), voice: v}).diagnostics).includes('voice-stale'));
});

test('files still being generated are placeholders in a draft and errors in the final check', () => {
  const media = {'assets/video/take_A.mp4': {exists: true}, 'assets/video/take_B_1.mp4': {exists: true}, 'assets/video/take_B_2.mp4': {exists: true}, 'assets/video/take_C.mp4': {exists: true}, 'assets/video/broll.mp4': {exists: false}};
  const draft = resolveEdit({edit: edit(), voice: voice(), media, draft: true});
  assert.deepEqual(errors(draft.diagnostics), []);
  assert.deepEqual(draft.pending, ['assets/video/broll.mp4']);
  assert.equal(draft.main[2].missing, true);
  assert.ok(errors(resolveEdit({edit: edit(), voice: voice(), media}).diagnostics).includes('asset-missing'));
});

test('layouts resolve every slot, takes included', () => {
  const e = edit({main: [{take: 'A'}, {layout: {type: 'pip', base: {video: 'screen.mp4'}, inset: {take: 'B'}}, until: {line: 'C'}}, {layout: {type: 'split', slots: [{take: 'C'}, {image: 'chart.png'}]}}], overlays: []});
  const r = resolveEdit({edit: e, voice: voice()});
  assert.deepEqual(errors(r.diagnostics), []);
  const pip = r.main[1].layout!;
  assert.equal(pip.type, 'pip');
  assert.equal(pip.type === 'pip' && pip.inset.src, 'assets/video/take_B_1.mp4');
  // The inset take changes parts at 9 s, so the whole item is cut there.
  assert.equal(r.main[2].id, 'm2.2');
  assert.equal(r.main[2].layout?.type === 'pip' && r.main[2].layout.inset.src, 'assets/video/take_B_2.mp4');
});

test('captions group into short phrases and never overlap', () => {
  const r = resolveEdit({edit: edit(), voice: voice()});
  const phrases = r.captions.groups.map((g) => g.words.map((w) => w.text).join(' '));
  assert.deepEqual(phrases.slice(0, 3), ['Be honest:', 'your pictures look fake.', 'Upload any picture.']);
  for (let i = 1; i < r.captions.groups.length; i++) assert.ok(r.captions.groups[i].from >= r.captions.groups[i - 1].to);
  assert.equal(resolveEdit({edit: edit({captions: {enabled: false}}), voice: voice()}).captions.enabled, false);
});

test('the director can set the caption phrases by hand', () => {
  const r = resolveEdit({edit: edit({captions: {phrases: ['Be honest', 'your pictures', 'look fake']}}), voice: voice()});
  const phrases = r.captions.groups.map((g) => g.words.map((w) => w.text).join(' '));
  assert.deepEqual(phrases.slice(0, 4), ['Be honest', 'your pictures', 'look fake', 'Upload any picture.']);
  const bad = resolveEdit({edit: edit({captions: {phrases: ['Be honest', 'our pictures']}}), voice: voice()});
  assert.ok(bad.diagnostics.some((d) => d.code === 'caption-phrase-mismatch'));
  const joined = takePhrases([{text: '24', start: 0, end: 0.2}, {text: 'MP', start: 0.2, end: 0.4}], ['24MP+']);
  assert.deepEqual(joined.groups[0].map((w) => [w.text, w.start, w.end]), [['24MP+', 0, 0.4]]);
});

test('scripts without spaces and right-to-left scripts are recognised', () => {
  assert.equal(isUnspaced('今日は良い天気です'), true);
  assert.equal(isUnspaced('hello world'), false);
  assert.equal(isRtl('مرحبا بالعالم'), true);
  const groups = groupWords('今日 は 良い 天気 です 明日 も 晴れ ます'.split(' ').map((t, i) => ({text: t, start: i * 0.3, end: i * 0.3 + 0.25})), {}, 99);
  assert.ok(groups.length >= 2 && groups.every((g) => g.spaced === false));
});

test('a script without spaces is cut into words, and its estimate uses them', () => {
  assert.deepEqual(tokenize('Two words.'), ['Two', 'words.']);
  const words = tokenize('今日は良い天気です。');
  assert.equal(words.join(''), '今日は良い天気です。');
  assert.ok(words.length >= 3 && words[words.length - 1].endsWith('。'));
  const e = edit();
  e.voice = {lines: [{id: 'A', text: '今日は良い天気です。明日も晴れます。'}]};
  e.main = [{color: '#000', until: {word: '明日'}}, {color: '#111', until: {voice: 'end'}}];
  e.overlays = [];
  const r = resolveEdit({edit: e});
  assert.deepEqual(errors(r.diagnostics), []);
  assert.equal(r.main.length, 2);
  assert.ok(r.captions.groups.length >= 1 && r.captions.groups.every((g) => g.spaced === false));
});

test('the edit is checked before anything is resolved', () => {
  assert.ok(errors(validateEdit({})).length > 0);
  assert.deepEqual(validateEdit(edit()), []);
  const codes = (e: unknown) => validateEdit(e).map((d) => `${d.level}:${d.where}`);
  assert.ok(codes({...edit(), main: [{take: 'Z'}]}).includes('error:main[0].take'));
  assert.ok(codes({...edit(), main: [{video: 'a.mp4', until: 1, for: 2}]}).includes('error:main[0]'));
  assert.ok(codes({...edit(), main: [{video: 'a.mp4', scene: 'X', until: 1}]}).includes('error:main[0]'));
  assert.ok(codes({...edit(), main: [{video: 'a.mp4', until: {word: 'x', line: 'Q'}}]}).includes('error:main[0].until'));
  assert.ok(codes({...edit(), main: [{video: 'a.mp4', untill: 3}]}).includes('warning:main[0].untill'));
  assert.ok(codes({...edit(), overlays: [{tag: 'a', card: 'b'}]}).includes('error:overlays[0]'));
  assert.ok(codes({...edit(), main: [{video: 'a.mp4', until: 2, enter: 'spin'}]}).includes('error:main[0].enter'));
});

test('the checker flags what fights for the same part of the frame', () => {
  const e = edit();
  e.overlays = [{tag: 'one', from: 1, until: 5}, {tag: 'two', from: 3, until: 7}, {tag: 'low', from: 1, until: 2, zone: 'lower'}];
  const r = resolveEdit({edit: e, voice: voice()});
  const codes = lintResolved(r, e).map((d) => d.code);
  assert.ok(codes.includes('overlay-overlap'));
  assert.ok(codes.includes('overlay-in-caption-zone'));
  const brief = edit();
  brief.overlays = [{tag: 'six words that nobody can read', from: 1, for: 0.5}];
  assert.ok(lintResolved(resolveEdit({edit: brief, voice: voice()}), brief).some((d) => d.code === 'text-too-brief'));
});

test('unreadable colour pairs are flagged, heavy type on a saturated card is not', () => {
  assert.ok(contrastRatio('#FFFFFF', '#000000')! > 20);
  assert.ok(contrastRatio('#FFFFFF', '#FFD400')! < 1.8);   // white on yellow: a real fix a shipped video needed
  assert.ok(contrastRatio('#FF5A5F', '#FFD400')! > 1.8);   // coral on yellow: what that video shipped with
  const r = resolveEdit({edit: edit(), voice: voice()});
  const codes = (pairs: {name: string; fg: string; bg: string}[]) => lintResolved(r, edit(), {contrastPairs: pairs}).map((d) => d.code);
  assert.ok(codes([{name: 'title', fg: '#fff', bg: '#FFD400'}]).includes('low-contrast'));
  assert.ok(!codes([{name: 'title', fg: '#121212', bg: '#FFD400'}]).includes('low-contrast'));
});

test('zones keep the proportions of a tall video on any canvas', () => {
  const tall = zonesFor(1080, 1920, 'tiktok');
  assert.deepEqual(tall.lower, {x: 90, y: 1160, w: 810, h: 400});
  assert.deepEqual(tall.upper, {x: 90, y: 120, w: 900, h: 580});
  const square = zonesFor(1080, 1080, 'none');
  assert.ok(square.lower.y + square.lower.h <= 1080 && square.upper.y >= 0);
});

test("Chrome gets a temp folder in memory only where that is safe", () => {
  const made: string[] = [];
  const probe = (over: Record<string, unknown> = {}) => ({
    platform: 'linux',
    env: {} as Record<string, string | undefined>,
    statfs: () => ({type: TMPFS_MAGIC, bavail: ROOM_BYTES / 4096, bsize: 4096}),
    mkdtemp: (prefix: string) => (made.push(prefix), `${prefix}Ab12Cd`),
    ...over,
  });
  // A memory filesystem with room: a folder of Chrome's own inside it.
  assert.equal(chromeTempDir(probe()), `${MEMORY_DIR}/kit-chrome-Ab12Cd`);
  assert.deepEqual(made, [`${MEMORY_DIR}/kit-chrome-`]);
  // The same answer when the numbers arrive as bigints.
  assert.equal(chromeTempDir(probe({statfs: () => ({type: BigInt(TMPFS_MAGIC), bavail: BigInt(ROOM_BYTES / 4096), bsize: 4096n})})), `${MEMORY_DIR}/kit-chrome-Ab12Cd`);
  // Everything else leaves Chrome where it is, and makes no folder.
  made.length = 0;
  assert.equal(chromeTempDir(probe({platform: 'win32'})), null);
  assert.equal(chromeTempDir(probe({platform: 'darwin'})), null);
  assert.equal(chromeTempDir(probe({env: {KIT_CHROME_TEMP: 'disk'}})), null);                              // switched off
  assert.equal(chromeTempDir(probe({statfs: () => ({type: 0xef53, bavail: 1e9, bsize: 4096})})), null);    // a disk mounted there
  assert.equal(chromeTempDir(probe({statfs: () => ({type: TMPFS_MAGIC, bavail: 16384, bsize: 4096})})), null);  // Docker's 64 MB
  assert.equal(chromeTempDir(probe({statfs: () => { throw new Error('ENOENT'); }})), null);                // no /dev/shm
  assert.deepEqual(made, []);
  assert.equal(chromeTempDir(probe({mkdtemp: () => { throw new Error('EACCES'); }})), null);               // not writable
  // Any other value of the switch is not "disk".
  assert.equal(chromeTempDir(probe({env: {KIT_CHROME_TEMP: 'memory'}})), `${MEMORY_DIR}/kit-chrome-Ab12Cd`);
});

test("only what is started inside inherits Chrome's temp folder", async () => {
  const env: Record<string, string | undefined> = {TMPDIR: '/tmp', OTHER: 'kept'};
  const seen = await withTempDir(env, '/dev/shm/kit-chrome-x', async () => env.TMPDIR);
  assert.equal(seen, '/dev/shm/kit-chrome-x');
  assert.deepEqual(env, {TMPDIR: '/tmp', OTHER: 'kept'});
  // Put back after a failure too, and removed again where there was none.
  const bare: Record<string, string | undefined> = {};
  await assert.rejects(withTempDir(bare, '/dev/shm/kit-chrome-y', async () => { throw new Error('no browser'); }), /no browser/);
  assert.equal('TMPDIR' in bare, false);
  // No folder: the environment is not touched at all.
  assert.equal(await withTempDir(env, null, async () => env.TMPDIR), '/tmp');
});
