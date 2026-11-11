---
name: voice-design
description: A video's voiceover in one voice designed for its presenter, with tool voiceover_tts (ElevenLabs Voice Design and Eleven v4 Turbo, a stock voice when no designed voice can be had). Read before the first voiceover_tts call - how to describe the voice, the one call that speaks every line, speaking a line again later, and what the tool answers.
---

# voiceover_tts - one voice for one video

`voiceover_tts` speaks the lines of a video in ONE voice. The first call of a video makes that
voice from your description of the speaker and speaks every line. The voice is then kept for the
video: a later call gives only the lines to speak again (while the video is made, or in a
revision) and gets them in the same voice.

## The first call: every line, once

```json
{
  "voice_description": "A woman in her early thirties who grew up in California. Bright, clear, friendly voice with natural conversational American English. She sounds like a creator talking to her followers on a phone video: quick, upbeat and genuine, never like an announcer. Natural breaths and small changes of pitch. Clean, close recording.",
  "gender": "woman",
  "age": "early 30s",
  "language": "en",
  "lines": [
    {"id": "A", "text": "<line A, exactly as in the edit>", "output_path": "<project>/assets/audio/A.mp3"},
    {"id": "B", "text": "<line B>", "output_path": "<project>/assets/audio/B.mp3"}
  ]
}
```

- One call, ALL lines. It takes about 15 seconds and about $0.07 for a 30 second script; put it
  at the head of the burst, beside the portrait. `kit voice` runs when it has returned.
- `gender` and `age` are the presenter's, as your plan states them. They pick the stock voice if
  one is needed, and they are added to the description when it does not say them.
- Every `output_path` ends in `.mp3` and is where `kit voice` looks: `assets/audio/<line id>.mp3`.
- Leave `model` out: the default is the designed voice on Eleven v4 Turbo.

## Writing the description

Describe the person who is speaking, in four or five plain sentences:

1. **Who:** age and gender, and where they are from.
2. **Accent:** the natural accent of the video's language (for English: American, unless the
   request or the brand says otherwise). Another accent ONLY when the request asks for it. The
   presenter's heritage is not an accent.
3. **The sound:** warm, bright, deep, husky, soft, slightly gravelly.
4. **How they talk:** a person talking to a friend on a phone video, at ease, never an announcer
   or a commercial narrator; quick or unhurried.
5. **The recording:** clean and close.

The presenter and the voice describe the same person: write them next to each other in the plan.
For a video in another language, write the description in English and say the language and where
the speaker is from ("a man in his forties from Madrid, native Castilian Spanish").

Never name a real person, a celebrity or a character, and never ask for "a voice like" one: the
design is refused, and it is not ours to use.

Three descriptions that gave voices the owner liked:

- "A man in his mid sixties from the American Midwest. Warm, slightly gravelly voice with an easy,
  unhurried rhythm. He sounds like a friendly neighbour talking to a friend on a phone video:
  relaxed, direct and genuinely pleased with what he found, never like an announcer or a
  commercial narrator. Natural breaths and small changes of pitch. Clean, close recording."
- "A Korean American woman in her early thirties who grew up in California. Bright, clear,
  friendly voice with natural conversational American English. She sounds like a creator talking
  to her followers on a phone video: quick, upbeat and genuine, never like an announcer or a
  commercial narrator. Natural breaths and small changes of pitch. Clean, close recording."
- (only because the request asked for the accent) "A woman in her early thirties from Seoul who
  speaks fluent English with a light, natural Korean accent. Bright, clear, friendly voice. She
  sounds like a creator talking to her followers on a phone video: upbeat and genuine, never like
  an announcer or a commercial narrator. Clean, close recording."

## What comes back

- `voice.kind`: `saved` (the designed voice, kept for this video) or `stock`.
- `fallback` and `note`, when a stock voice spoke instead: no designed voice could be had this
  time (the month's voices are used up, no room, a refused description, ElevenLabs was slow).
  The lines are all there and all in that one stock voice. Carry on; do not call again to "get
  the designed voice", and do not mention it to the client.
- `lines`: each line's file. `cost_usd` is what the call cost.

## Speaking a line again

Call the tool with just the lines to speak again (`id`, `text`, `output_path`), no description:

```json
{"lines": [{"id": "C", "text": "<the new line C>", "output_path": "<project>/assets/audio/C.mp3"}]}
```

It uses the video's own voice, designed or stock. Then run `kit voice` again and remake the take
of that line.

If it answers that **the video's voice no longer exists**, leave the spoken words as they are:
change what else was asked (captions, on-screen text, timing, music, pictures) and say in the
summary that the spoken words stayed. Do not speak one line in another voice.

`new_voice: true` gives the video a new voice. Every line must then be spoken again and every
take remade, so use it only when the client asks for a different voice.

## Never

- Never speak a video's lines with two tools or two voices.
- Never call `fal_elevenlabs_tts` for a line of a video that `voiceover_tts` has spoken. (A video
  made before designed voices names its stock voice in its plan: there, and only there, a changed
  line is spoken with `fal_elevenlabs_tts` and that voice.)
- Never trim, pad or split a line's audio yourself: `kit voice` does it.
