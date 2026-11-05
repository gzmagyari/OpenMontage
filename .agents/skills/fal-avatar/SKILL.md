---
name: fal-avatar
description: Acted, lip-accurate talking takes from one portrait plus a TTS audio file via fal.ai (H3 Max two-pass composite), plus single-pass avatar models and lip-sync repair of existing speech videos. Read before any fal_avatar call.
---

# fal_avatar - audio-first talking takes

The presenter's voice is an audio FILE you provide (make it with a TTS tool first, one fixed
voice per person for the whole video). `fal_avatar` turns that file plus one portrait into a
talking take whose delivered soundtrack is that file **verbatim** - so the voice can never
drift between takes, which is the main failure mode of video models that invent their own
speech.

## Modes

| Mode | Inputs | What it does | Cost | Wall time |
|---|---|---|---|---|
| `model: "h3max-acting"` (default) | `image_*`, `audio_*`, `prompt` | H3 Max acting plate conditioned on the audio -> H3 Max lip-sync pass -> re-mux of the exact audio | ~$0.085/s | ~2 min |
| lip-sync fix | `video_*`, `audio_*` | Re-syncs the lips of ANY existing speech video to the audio, then re-muxes it | ~$0.05/s | ~1.5 min |
| single-pass avatars | `image_*`, `audio_*`, `model: kling-v2-*` / `veed-fabric-1.0` / `omnihuman-1.5` | Classic audio-driven avatar; synced but calmer, little free acting | $0.056-0.16/s | 2-4 min |

Use `h3max-acting` for presenter takes that should feel like a real creator video (gestures,
lean-ins, energy). Use a single-pass avatar when a calm, steady talking head is wanted. Use
the fix mode to repair a take or dub existing footage.

## Call pattern (composite)

```json
{
  "model": "h3max-acting",
  "image_path": "<9:16 medium close-up of the person; face ~40% of frame height>",
  "audio_path": "<this take's speech audio>",
  "trim_silence": true,
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

- One take = at most **15 s of audio** (hard cap of the plate). Split longer speech at sentence breaks.
- One take = at least **~4.5 s of audio**: the lip-sync pass rejects videos under 5 s (fal 422),
  and a shorter line yields a 4 s plate. For a shorter line, tail-pad the audio to 5 s
  (`apad=whole_dur=5`) BEFORE the call and trim the delivered clip back to the spoken length after.
- ElevenLabs Eleven v3 inserts long dramatic pauses; pass `trim_silence: true` (a verified ffmpeg
  chain inside the tool). **Never hand-roll the trim** - a one-character `afade` mistake once
  silenced a full production run.
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
- H3 Max i2v `target_audio_url` **alone** is not accurate lip-sync (it conditions the acting
  loosely; with silent audio the mouth goes dead). That is exactly why the composite runs the
  dedicated lip-sync pass - do not skip it for on-camera speech.
