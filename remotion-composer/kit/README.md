# The video kit

An edit file in, a rendered video out. The director decides what is on screen and when; the
kit supplies everything that is the same in every video and was being rewritten each time:
shot sequencing, cuts pinned to spoken words, transitions, clip playback, captions, safe
zones, the soundtrack, the export.

Three layers and an escape hatch:

- **Engine** (`engine/`) - imported, never edited by a job. General building blocks only.
- **Look** (`looks/<name>/`) - copied into each job as `composition/src/look/`. Yours to
  restyle and rewrite; `kit check` refuses a final render while it is untouched.
- **The edit** (`composition/edit.json`) - the shot list as data.
- **Your own scenes** - any main item or overlay can be a component written for this video.

## A job's files

```
composition/
  edit.json          the edit
  src/index.ts       registerRoot(makeRoot({look, scenes}))   (staged, leave as is)
  src/look/          tokens.ts (colours, type, shape) and look.tsx (the components)
  src/scenes.ts      registry of scenes written for this video
  src/scenes/*.tsx   those scenes
work/kit/            voice.json, check.json, sheet.jpg, the bundle (all generated)
```

## Commands (run in the project directory)

| Command | What it does |
|---|---|
| `kit init [--look NAME]` | Stage `composition/`. Never overwrites what is there. |
| `kit look [NAME] [--force]` | List the starter looks, or copy one into the job. |
| `kit voice` | After the lines are spoken: tighten each recording, time every word of the script, split a line that is too long for one take, lay the lines end to end. |
| `kit check [--draft]` | Check the edit, the timeline and the look. `--draft` accepts files that are still generating. |
| `kit sheet [--draft]` | One sheet of the telling frames (about 8 s), plus layout findings measured in the browser. |
| `kit mix` | The soundtrack: voice on the spine, music under it (or the music alone when there is no voice), cut to the video's length and faded out, -14 LUFS. |
| `kit render [--name NAME]` | The final video in `exports/`, tagged BT.709, verified. Refuses while anything is missing. |

Recordings are found at `assets/audio/<line id>.mp3` (or `.wav`), or wherever the line's
`audio` says. `kit voice` writes `assets/audio/voice_<part>.wav`: generate each take from
that file exactly as it is (no further trimming), so the take carries the audio the timings
were made from. It prints the list and writes it to `work/kit/voice.json`:
`lines[].parts[]`, each with `id`, `audio` (the prepared file), `take` (where its take belongs,
when the line has one), `start` and `dur`. A line over the take limit comes back as two parts,
so read the takes to film from this file, not from the script.

## The edit

```json
{
  "canvas": {"size": "1080x1920", "fps": 30, "safe": "tiktok"},
  "voice": {"lines": [{"id": "A", "take": true, "text": "The exact words of the line."}]},
  "music": {"file": "assets/audio/music.mp3"},
  "captions": {"zone": "lower"},
  "main": [
    {"take": "A", "until": {"word": "words"}},
    {"video": "assets/video/broll.mp4", "until": {"line": "A", "edge": "end"}, "enter": "punch"},
    {"freeze": "A", "for": 2}
  ],
  "overlays": [
    {"tag": "up to 4x", "from": {"word": "exact"}, "for": 1.6},
    {"card": "end", "props": {"title": "example.com"}, "from": {"voice": "end"}}
  ]
}
```

`voice.lines` holds every spoken line in order, with its exact words. `"take": true` means a
presenter is filmed saying it (`{"take": "A"}` then shows it); leave `take` out for a
voice-over that is only heard. A video with no voice has no lines: give every main item `for`
(or `until` in seconds), the video is as long as its items, and the soundtrack is the music.

### Moments

Anywhere a time is wanted:

| Form | Means |
|---|---|
| `3.5` or `{"t": 3.5}` | seconds from the start |
| `{"word": "rebuilds"}` | the next time the voice says it (a short phrase works too). In `main`, "next" counts from where the item starts; in overlays, from the start of the video. Add `"line"`, `"nth"` or `"edge": "end"` to pick. |
| `{"line": "B"}` | the start of line B (`"edge": "end"` for its end) |
| `{"voice": "end"}` | where the last line ends |
| `{"item": "m3"}` | the start of a main item (by `id`, or `m<n>` by position) |
| `{"end": true}` | the end of the video (overlays only) |

Every form takes `"offset"` in seconds. Words match whatever their case, punctuation or
hyphens. Before `kit voice` has run, timings are estimated from the script so an edit can be
checked and sheeted straight away.

### main: what fills the frame, in order

Each item starts where the one before it ended and runs `until` a moment or `for` seconds.
A take with neither runs to the end of its line; the last item runs to the end of the voice.

| Item | Shows |
|---|---|
| `{"take": "A"}` | the presenter take of line A, always on the frame that matches the voice, also after a cutaway |
| `{"video": "..."}` | a clip (`"start"` seconds in, `"speed"`, `"audio"` 0-1 to keep its sound) |
| `{"image": "..."}` | a still, with a slow push-in |
| `{"scene": "Name", "props": {...}}` | a ready-made scene or one of your own |
| `{"freeze": "A"}` | the last frame of line A's take (or of a video path) |
| `{"color": "#101010"}` | a plain colour |
| `{"layout": {...}}` | several of the above at once (below) |

Optional on any item: `"enter"` (`cut`, `fade`, `slide-left|right|up|down`, `whip`, `punch`,
`wipe`, `flash`, or `{"type", "frames"}`), `"motion"` (`push-in`, `pull-out`, `pan-left|right|up|down`,
`drift`, `hold`, `none`, or `{"from", "to", "origin"}`), `"fit"` (`cover`, `contain`, `blur`), `"id"`.
The incoming shot enters on top of the outgoing one, so a transition never moves a cut.

Layouts:

```json
{"layout": {"type": "split", "slots": [{"take": "A"}, {"video": "screen.mp4"}]}}
{"layout": {"type": "pip", "base": {"video": "screen.mp4"}, "inset": {"take": "A"}, "corner": "bottom-left"}}
{"layout": {"type": "card", "media": {"image": "wide.jpg"}, "aspect": 1.5}}
{"layout": {"type": "grid", "columns": 2, "slots": [...]}}
```

### overlays: what sits on top

`{"tag": "text"}`, `{"card": "name", "props": {...}}`, `{"text": "..."}` or
`{"scene": "Name", "props": {...}}`, each with `from` and `until` (or `for`) and an optional
`zone` (`upper`, `center`, `lower`, `full`). A tag sits in the upper zone, a `text` (a large
statement; "\n" starts a new line) in the centre. Which cards exist and what props they
take is the look's business: read `src/look/look.tsx`.

### captions

On whenever there is a voice. `{"enabled": false}` turns them off. `maxWords` / `maxChars`
tune the automatic grouping; `"phrases": ["Your AI images", "look AI", ...]` sets the breaks
(and the spelling shown) by hand. Scripts without spaces and right-to-left scripts are
handled.

## Looks

`pop` - chunky creator style: hard offset shadows, one loud accent, heavy type, springy pops.
Cards: `hook`, `cta`, `end`.

`editorial` - ink, paper chips and one signal colour; calm entrances. Cards: `end`.

A look exports `look` (see `engine/context.tsx` for the shape: `Caption`, `Tag`, `Text`,
`cards`, optional `Finish`, `Frame`, `parts`, `motion`) and, in `tokens.ts`, `tokens` and
`meta` (card zones, caption grouping, the colour pairs the legibility check reads).
`tokens.ts` must not import anything: the kit command reads it without a bundler.

Every look names the same roles, so ready-made scenes dress themselves in any look:
`color.ink`, `color.paper`, `color.accent` (plus `negative`, `positive`), `font.display`
(plus `body`, `weight`) and `shape` (`radius`, `border`, `offset`, `shadow: "hard" | "soft"`).
Keep those names when restyling; add whatever else the look needs.

Fonts. The kit ships eleven families, loaded from local files (no network): call the one a
look uses once at the top of the look and use the name it returns, for example
`const FONT = fonts.anton();` with `fonts` from `@kit/engine`.

| | |
|---|---|
| Heavy display | `anton`, `archivoBlack`, `bebasNeue` |
| Sans, any weight | `inter`, `montserrat`, `poppins` (500, 700, 800, 900), `spaceGrotesk` |
| Serif | `dmSerifDisplay`, `playfair`, `playfairItalic` |
| Handwritten | `caveat` |

Installed on the render box and usable by name without loading: DejaVu Sans / Serif / Mono,
Noto Sans, Noto Sans Display, Noto Serif, Noto Serif Display (regular and bold), and the Noto
families for scripts beyond Latin (captions in any script fall back to them by themselves).

## Ready-made scenes

Each fills the frame as a main item; `ListReveal`, `QuoteCard` and `StatCount` also work as
overlays (`{"scene": "...", "zone": "upper"}` over footage). `backdrop` is a colour, the path
of a picture or clip, or `{"image" | "video" | "color", "blur", "dim"}`.

`BeforeAfter` - two pictures and a divider that wipes from one to the other. Props: `pair`
("refs/gran" means `refs/gran_before.jpg` and `refs/gran_after.jpg`) or `before` + `after`;
`labels`; `afterSide`; `wipe` (`start`, `seconds`, `from`, `to`); `zoom`; `frame: "card"` for
landscape pictures in a tall video. The handle and labels are drawn by the look.

`KineticText` - big words that arrive one after another; a statement that needs no footage.
Props: `text` ("\n" starts a line) or `lines`; `emphasis` (words in the accent colour);
`backdrop`; `align`; `size`; `uppercase`.

`ScreenDemo` - a screenshot or screen recording in a browser or phone frame, with a camera, a
cursor, clicks and outlined areas. Props: `image` or `video`; `frame` (`browser`, `phone`,
`plain`); `url`; `steps`, each `{"at": seconds, "zoom", "focus": [x, y], "highlight":
[x, y, w, h], "cursor": [x, y], "click", "label"}` with positions as fractions of the screen
content.

`ListReveal` - a short list whose points arrive one by one. Props: `items`; `title`; `at`
(seconds into the shot, or the word the voice says as each point lands); `numbered`.

`QuoteCard` - what a customer said. Props: `quote`, `name`, `role`, `stars`, `avatar`.

`StatCount` - a number that counts up. Props: `value`, `prefix`, `suffix`, `decimals`,
`label`, `seconds`.

## Your own scenes

```tsx
// composition/src/scenes/Orbit.tsx
import {useCurrentFrame} from 'remotion';
import {PushIn, useShot, useWordFrame} from '@kit/engine';

export const Orbit: React.FC<{image: string}> = ({image}) => {
  const frame = useCurrentFrame();          // 0 at the start of this shot
  const {frames} = useShot();               // how long the shot is on screen
  const atWord = useWordFrame('rebuilds');  // the frame the voice says it, or null
  ...
};
```

Register it in `src/scenes.ts` (`export const scenes = {Orbit};`) and name it in the edit.
Media paths in props are project-relative and are staged and checked like any other file.
`useDress()` gives the look's colours, type and surface style, `surface(d)` a card in the
look's manner, `Backdrop` what sits behind. A scene must never be empty on its first frame
(a video's first frame has to work on its own): start its entrance `HEAD_START` frames in,
as the ready-made scenes do.

## What `check` and `sheet` look for

Shape of the edit; words that are not in the script (with close matches); moments that fall
before the item starts; lines too long for one take (estimated before any voice is bought);
missing files; frames with nothing on screen; shots under 12 frames; text on screen too
briefly to read; overlays sharing a zone or sitting in the caption zone; colour pairs that
are hard to read; a look that was never restyled; and, measured on the sheet's frames,
overlays that reach into what the app covers or over the captions.

## Tests

`node --test remotion-composer/kit/test/engine.test.ts` (the engine's logic) and
`pytest tests/kit` (voice, staging, the engine's tests, and a small video built end to end).
