# RUN_REPORT - photorealify UGC TikTok ad

## What was produced
- Final video: `/data/projects/job_cfcebe356b5cb69e/exports/photorealify_ugc_tiktok_1080x1920.mp4`
- Specs (ffprobe): 1080x1920, H.264 High, yuv420p, 30 fps, BT.709 limited range (tv), AAC 48 kHz, duration 13.61 s (target was about 15 s; the script is about 10 s of speech plus a 3.5 s CTA card).
- Audio: voice and music mixed with ducking; measured -14.0 LUFS integrated, -2.2 dBTP peak.
- Structure: 0-4.4 s presenter (hook text "AI photos still look fake?") -> 4.4-7.6 s BEFORE/AFTER cutaways with voice continuing -> 7.6-10.1 s presenter (CTA line) -> 10.1-13.6 s end card "photorealify.com".
- Script (verified against transcript): "My AI pictures always looked fake. Then I found photorealify. It turns AI-looking images into realistic photos. Check out the difference. Link is in my bio."
- Captions: 26 word-level captions burned in via Remotion; they match the script exactly.

## Paid calls and cost
| Item | USD |
|---|---|
| fal_elevenlabs_tts eleven-v3 voiceover (156 chars, Rachel) | 0.0156 |
| fal_avatar h3max-acting take1 (10.0 s audio: plate 0.3515 + lipsync 0.5021) | 0.8536 |
| fal_elevenlabs_music 15 s instrumental bed (billed 1 min at 0.60) | 0.6000 |
| **Total** | **1.4692** |

Budget: $5.00 hard cap in the locked decisions. Total paid spend is $1.4692, under the cap. No reshoot was needed.

## Wall time (approximate)
- Preflight, reading guide and director skills: ~5 min
- Assets (TTS, avatar take, music): ~5 min total; the avatar call dominated (~2-3 min)
- Review frames and audio probe: ~1 min
- Mix and ducking: ~1 min (two passes for loudness)
- Remotion render + encode: ~2 min

## Problems and solutions
1. `.venv` not present in /app and the Windows `.venv/Scripts` path does not exist here. Used system `python`.
2. Presenter reference is 1365x2048 (2:3), not 9:16. Cropped a 1152x2048 region centered on the face and scaled to 1080x1920.
3. Pixabay music search returned HTTP 403. Used fal_elevenlabs_music (instrumental, 15 s, billed as one minute at $0.60).
4. faster-whisper was not installed, so the transcriber failed. Installed it with pip (no paid cost). The first transcriber call did not write its JSON output, so I re-ran it and saved the output myself.
5. Whisper heard "Photo Realify". Captions use the scripted "photorealify" with the same word timings, so captions still match the audio.
6. TalkingHead registers at a fixed 300 s, and `video_compose remotion_render` has no frame range, which would have produced a 5-minute render. Called the Remotion CLI directly on the TalkingHead composition with `--frames=0-406`, the props file, and the staged public dir. No repo code was changed.
7. The Remotion output was yuvj420p (full range). Converted to BT.709 limited range with ffmpeg in the final encode.
8. First mix measured -14.6 LUFS with a -14 target. Re-ran the mix with a -13.4 loudnorm target, which measured -14.0 LUFS.
9. Take 1 passed review (identity holds, mouth moving mid-take, audio peak -0.5 dB), so no reshoot was spent.

## Choices made without asking (unattended run)
- Single voice: Rachel via fal ElevenLabs eleven-v3.
- Presenter take: fal_avatar h3max-acting with trim_silence on.
- BEFORE/AFTER cutaways and the end card were built as ffmpeg cards from the references; the presenter take audio carries the whole voiceover.
- All human approval gates were auto-approved per the unattended-run instruction; each choice is recorded in `artifacts/decision_log.json`.

## Files
- Cost log: `artifacts/cost_log.json`
- Decision log: `artifacts/decision_log.json`
- Transcript: `artifacts/transcript_take1.json`
- Remotion props: `artifacts/remotion_props.json`
- Presenter take: `assets/video/take1_raw.mp4`; voice: `assets/audio/vo_raw.mp3`; mix: `assets/audio/final_mix_b.wav`; music bed: `assets/music/bed_fal.mp3`
- Review frames: `review/`
