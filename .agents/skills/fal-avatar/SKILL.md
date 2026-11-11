---
name: fal-avatar
description: Acted talking takes from one portrait plus a prepared audio file via fal.ai (H3 Max Turbo with native lips by default, the full H3 Max model or a frame-accurate lip-sync pass on request), plus single-pass avatar models and lip-sync repair of existing speech videos. Read before any fal_avatar call.
---

# fal_avatar - audio-first talking takes

The presenter's voice is an audio FILE you provide (make it first: in a video job with
`voiceover_tts`, one voice designed for the presenter; elsewhere with a TTS tool, one fixed
voice per person for the whole video). `fal_avatar` turns that file plus one portrait into a
talking take whose delivered soundtrack is that file **verbatim** - so the voice can never
drift between takes, which is the main failure mode of video models that invent their own
speech.

## Modes

| Mode | Inputs | What it does | Cost | Wall time |
|---|---|---|---|---|
| `model: "h3max-turbo"` (default) | `image_*`, `audio_*`, `prompt` | ONE H3 Max Turbo acting generation conditioned on the audio; its own lips ship; trimmed to the line and re-muxed with the exact audio | $0.028/s at 768P | ~1 min |
| `model: "h3max"` | `image_*`, `audio_*`, `prompt` | The same take on the full H3 Max model. Same inputs, same clip lengths, twice the price; the two were judged alike on the same line (2026-11-10) | $0.056/s at 768P | ~1 min |
| `model: "h3max-acting"` | `image_*`, `audio_*`, `prompt` | An H3 Max plate, then an H3 Max lip-sync pass for frame-accurate lips, then the re-mux | ~$0.26/s | ~2 min |
| lip-sync fix | `video_*`, `audio_*` | Re-syncs the lips of ANY existing speech video to the audio, then re-muxes it. Also the upgrade for a delivered take whose lips are not good enough | +$0.20/s | ~1.5 min |
| single-pass avatars | `image_*`, `audio_*`, `model: kling-v2-*` / `veed-fabric-1.0` / `omnihuman-1.5` | Classic audio-driven avatar; synced but calmer, little free acting | $0.056-0.16/s | 2-4 min |

Use `h3max-turbo` for presenter takes: it acts like a real creator video (gestures, lean-ins, energy)
and its lips are good enough for standard shots. Reserve `h3max-acting` for tight close-ups and
lip-critical shots. When QC rejects a delivered take's LIPS, run the fix mode on that take
instead of regenerating it. Use a single-pass avatar when a calm, steady talking head is wanted.
Fal bills whole seconds, rounded up; an H3 Max plate is billed for the seconds it is asked for,
which is the fewest whose clip holds the line (4 s for a 4.1 s line, 8 s for a 7.4 s one).

## Call pattern

```json
{
  "model": "h3max-turbo",
  "image_path": "<9:16 medium close-up of the person; face ~40% of frame height>",
  "audio_path": "<this take's PREPARED audio: assets/audio/voice_<part>.wav from `kit voice`>",
  "prompt": "Vertical 9:16 selfie video filmed on a phone front camera held at arm's length, one continuous shot, no cuts, natural handheld micro-movement, photorealistic, real skin texture. The woman from the first frame (<stable physical traits only>) <one natural action>, leans toward the lens and talks straight into the camera with playful, confident energy and natural small hand gestures, her lips precisely following the words of the soundtrack. <setting and lighting matching the photo>. No music, no subtitles, no on-screen text.",
  "output_path": "<take.mp4>"
}
```

Prompt rules (learned the expensive way):
- Describe the person with **stable traits only** (hair, eyes, jewellery, clothing). An expression
  or pose in the description ("a soft closed-lip smile") freezes the face.
- Attach the speech to the person and one movement; keep "talks straight into the camera".
- A medium close-up start frame (face ~40% of frame height) lip-syncs far better than a wide crop.

## The audio contract

- **In a video job the audio you pass is the prepared file `kit voice` made** for this take
  (`assets/audio/voice_<part>.wav`), exactly as it is, with `trim_silence` off. `kit voice`
  already tightened it with the tool's own verified chain, and the word timings every cut and
  caption hangs on belong to that file: a take made from the raw line, or trimmed again, drifts
  out of sync. Save the take at the path `kit voice` lists beside the audio.
- One take = at most **15 s of audio** (hard cap of the plate). `kit voice` splits a longer line
  at a sentence pause and lists the parts; never split a line yourself.
- `h3max-acting` and the fix mode need at least **~4.5 s of audio**: the lip-sync pass rejects
  videos under 5 s (fal 422). The default `h3max-turbo` (and `h3max`) has no such floor.
- Outside the kit (no `kit voice`), ElevenLabs Eleven v3 output needs its long pauses tightened:
  pass `trim_silence: true` (the verified ffmpeg chain inside the tool). **Never hand-roll the
  trim** - a one-character `afade` mistake once silenced a full production run.
- The tool refuses audio whose peak is near zero *before* spending, and verifies the delivered
  clip is audible. If it refuses, the audio really is broken: fix the audio, do not force it.
- The output soundtrack is your input audio byte-for-byte (the tool re-muxes it); never replace
  or re-encode it afterwards.

## Review every take before using it

- Extract frames at the start and middle: identity must hold, the mouth must be moving during
  speech, and nothing may smear when a hand passes the face.
- Energy metrics (lip activity %, correlation) **cannot** detect a mouth speaking the wrong
  words - judge sync by watching frames against the audio, not by numbers.
- Reshoot a bad take at most once (~$0.90 for a 10 s composite take); prefer a reshoot over
  keeping a bad take. The fix mode can also repair a take whose acting is good but lips are off
  for ~$0.50.

## What NOT to do (verified dead ends)

- **Never use a video model's own generated speech** for presenter lines (Kling v3 native
  dialogue, Veo audio, Gemini Omni audio): each take invents a voice, voices drift between
  takes, and lips start late unpredictably.
- **Never use H3 Max / Hailuo `reference_audio_urls` for speech**: reference audio is a *style*
  reference - the model re-voices the words in a similar voice (drift again).
- **Never call H3 Max i2v with `target_audio_url` yourself** for a talking take; go through
  `fal_avatar`. The model returns clips of fixed lengths only (22 + 17n frames at 24 a second,
  so up to 0.7 s longer than asked) and lays the performance of the audio over the WHOLE clip.
  Audio shorter than the clip is acted slower than it sounds: a 4.1 s line in a 5.2 s clip
  moved its lips at 80% of the voice's pace and was a full second behind by its last word.
  That, not the model's lips, is what made native takes look out of sync until 2026-11-10.
  `fal_avatar` sends the line followed by silence up to exactly the clip's length, so nothing
  is stretched (the mouth rests through that silence), and cuts the take at the line's end.
  A track that is silent throughout gives a dead mouth, which is why the tool refuses one.
- If a take's answer carries a `note` that the model returned a clip of another length, the
  tool played that clip faster to keep the lips on the words. Watch that take before using it.
