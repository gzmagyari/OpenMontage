"""A video's spoken lines in ONE voice made for its presenter.

The first call of a video designs a voice from a description (ElevenLabs Voice Design), saves
it, and speaks every line with Eleven v4 Turbo. The voice is KEPT for as long as the video is
kept, so a later call for the same video (a line spoken again, a revision) gives only the lines
and gets them in the same voice, with no new design. The provider removes the voice with its
video (engine/jobs/manager.py); a new video that finds no free slot removes the oldest of our
voices first (lib/eleven_voices.py).

When ElevenLabs cannot give a voice (no key, the month's voice creations used up, no slot, a
refusal, an error, too slow) the call falls back by itself to a stock voice on fal, picked by
the presenter's gender and age. A video never has two voices: once the fallback is taken, every
line is spoken again in the stock voice, and later calls keep to it.

Measured on a real account, 2026-11-10 and 11:
- design 7 to 9 s (three candidates; the first is taken), save 2.3 s, a line 0.5 to 1.0 s on v4
  Turbo and 1.2 to 4.8 s on v4;
- a design counts the characters of its sample text once and answers without a cost header;
  every speech answer carries `character-cost` (about 0.115 a character on v4 Turbo, 0.23 on
  v4) and `request-id`;
- the plain speech endpoint takes both v4 models;
- saving a voice counts against a monthly limit even after the voice is removed, so the limit
  is checked before anything is spent on a design.

Costs are kept in ElevenLabs' own units and turned into dollars by one server setting,
ELEVENLABS_USD_PER_1K_UNITS (default 0.35: the published $0.08 per 1,000 characters of v4 is
about 230 units).
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from lib import eleven_voices as voices
from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

GONE = ("This video's voice no longer exists (a voice is removed with its video, or earlier to make "
        "room for newer videos). Leave the spoken words as they are; to change them anyway, speak "
        "EVERY line again with `new_voice: true` and remake the takes of the lines.")


class _Failed(Exception):
    """The call cannot be done at all; its words go to the caller."""


class _Fallback(Exception):
    """No designed voice this time; its words say why. The stock voice takes over."""


class _VoiceGone(Exception):
    """The saved voice is not on ElevenLabs any more."""


class _Spend:
    """What one call used, safe to add to from the threads that speak the lines."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.design_units = 0
        self.speech_units = 0
        self.eleven_ids: list[str] = []
        self.stock_usd = 0.0
        self.fal_ids: list[str] = []

    def speech(self, units: int, request_id: str | None) -> None:
        with self._lock:
            self.speech_units += units
            if request_id:
                self.eleven_ids.append(request_id)

    def stock(self, usd: float, request_ids: list[str]) -> None:
        with self._lock:
            self.stock_usd += usd
            self.fal_ids.extend(request_ids)

    @property
    def units(self) -> int:
        return self.design_units + self.speech_units


class VoiceoverTTS(BaseTool):
    """Every spoken line of a video in one voice designed for its presenter."""

    name = "voiceover_tts"
    version = "0.1.0"
    tier = ToolTier.VOICE
    # Not "tts": the selector for single lines must never route a line here.
    capability = "voiceover"
    provider = "elevenlabs"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = []
    install_instructions = (
        "Set ELEVENLABS_VOICE_API_KEY for voices designed per video (https://elevenlabs.io; the key needs "
        "voice design, voices, speech and the account read). ELEVENLABS_API_KEY is read too, but that name "
        "also switches on the direct ElevenLabs tools. Without a key, or when the account has no voice left "
        "to give, the lines are spoken by a stock voice through fal.ai, which needs FAL_KEY."
    )
    fallback_tools = ["fal_elevenlabs_tts"]
    agent_skills = ["voice-design"]

    capabilities = [
        "text_to_speech",
        "voice_design",
        "one_voice_per_video",
    ]
    supports = {
        "voice_cloning": False,
        "multilingual": True,
        "offline": False,
        "designed_voice": True,
    }
    best_for = [
        "the whole voiceover of a video in a voice that fits its presenter (gender, age, accent, tone)",
        "speaking one changed line later in the same voice (a revision)",
    ]
    not_good_for = [
        "a single stray line outside a video project",
        "the voice or likeness of a real person",
    ]

    # composite key -> (ElevenLabs speech model, units a character)
    MODELS: dict[str, tuple[str, float]] = {
        "v4-turbo-designed": ("eleven_v4_turbo", 0.115),
        "v4-designed": ("eleven_v4", 0.23),
    }
    DEFAULT_MODEL = "v4-turbo-designed"
    STOCK = "stock"

    input_schema = {
        "type": "object",
        "required": ["lines"],
        "properties": {
            "lines": {
                "type": "array",
                "minItems": 1,
                "maxItems": 60,
                "description": "The lines to speak. The video's FIRST call gives every line of the script; "
                               "a later call gives only the lines to speak again",
                "items": {
                    "type": "object",
                    "required": ["id", "text", "output_path"],
                    "properties": {
                        "id": {"type": "string", "description": "The line's id in the edit (A, B, ...)"},
                        "text": {"type": "string"},
                        "output_path": {"type": "string",
                                        "description": "Where the line's MP3 goes, e.g. assets/audio/A.mp3"},
                    },
                },
            },
            "voice_description": {
                "type": "string",
                "description": "First call only: who is speaking. Age, gender, where they are from (the natural "
                               "accent of the video's language unless the client asked for another), the "
                               "sound of the voice and how they talk (a person talking to a friend on a "
                               "phone video, never an announcer). Never the name or likeness of a real person",
            },
            "gender": {"type": "string", "enum": ["woman", "man"], "description": "First call only: the presenter's"},
            "age": {"type": "string", "description": "First call only: the presenter's, as in the plan (\"early 30s\", \"60s\")"},
            "language": {"type": "string", "default": "en", "description": "The language the lines are in (ISO 639-1)"},
            "model": {
                "type": "string",
                "default": "v4-turbo-designed",
                "enum": ["v4-turbo-designed", "v4-designed", "stock"],
                "description": "v4-turbo-designed = THE DEFAULT: a voice designed for this video, spoken with "
                               "Eleven v4 Turbo. v4-designed = the same with Eleven v4 (twice the speech "
                               "price, slower). stock = a stock voice chosen by gender and age",
            },
            "new_voice": {
                "type": "boolean",
                "default": False,
                "description": "Give the video a NEW voice although it has one. Every line must then be given "
                               "and spoken again, and the takes of all lines remade",
            },
            "project_dir": {"type": "string", "description": "The video's project folder, when it cannot be told "
                                                             "from the output paths"},
        },
    }

    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=50, network_required=True)
    retry_policy = RetryPolicy(max_retries=0, retryable_errors=[])
    idempotency_key_fields = ["lines", "voice_description", "gender", "age", "model"]
    side_effects = [
        "writes one MP3 per line",
        "on a video's first call: saves a voice on the ElevenLabs account (one of the month's voice creations) "
        "and writes artifacts/voice.json in the project",
        "may remove the oldest saved voice of this installation to make room",
    ]
    user_visible_verification = ["Listen to one line: the voice should sound like the presenter looks"]

    _DESIGN_MODEL = "eleven_ttv_v3"
    _FORMAT = "mp3_44100_128"
    # The design's sample text: ElevenLabs takes no less than 100 characters; we give no more than
    # a sentence past that (the trial's voices were designed on 156).
    _SAMPLE_MIN, _SAMPLE_AIM = 100, 240
    # The designed path gets this long before the stock voice takes over.
    _DESIGNED_SECONDS = 75.0
    # The account answers 429 above three requests at once (smallest plan); two leave one free.
    _AT_ONCE = 2
    _USD_PER_1K_UNITS = 0.35
    _STOCK_MODEL = "eleven-v3"
    _STOCK_USD_PER_CHARACTER = 0.0001
    _RECORD = Path("artifacts") / "voice.json"
    # Stock voices by the labels ElevenLabs gives them (read from an account, 2026-11-10), all
    # American; only names that fal's speech endpoint lists.
    _STOCK_VOICES: dict[tuple[str, str], tuple[str, ...]] = {
        ("woman", "young"): ("Jessica", "Sarah", "Laura"),
        ("woman", "middle"): ("Matilda",),
        ("woman", "old"): ("Matilda",),
        ("man", "young"): ("Liam", "Will"),
        ("man", "middle"): ("Brian", "Chris", "Eric"),
        ("man", "old"): ("Bill",),
    }
    _STOCK_BRITISH = {"woman": "Alice", "man": "George"}

    # ---- what is here ---------------------------------------------------------------------

    def get_status(self) -> ToolStatus:
        if voices.api_key() or os.environ.get("FAL_KEY") or os.environ.get("FAL_AI_API_KEY"):
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    @classmethod
    def _usd(cls, units: float) -> float:
        try:
            rate = float(os.environ.get("ELEVENLABS_USD_PER_1K_UNITS") or cls._USD_PER_1K_UNITS)
        except ValueError:
            rate = cls._USD_PER_1K_UNITS
        return units * rate / 1000

    @staticmethod
    def _lines(inputs: dict[str, Any]) -> list[dict[str, str]]:
        raw = inputs.get("lines")
        if not isinstance(raw, list) or not raw:
            raise ValueError("lines is required: a list of {id, text, output_path}")
        lines, seen = [], set()
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("each line is an object with id, text and output_path")
            line_id, text, path = (str(item.get(k) or "").strip() for k in ("id", "text", "output_path"))
            if not line_id or not text or not path:
                raise ValueError("each line needs an id, a text and an output_path")
            if not path.lower().endswith(".mp3"):
                raise ValueError(f"line {line_id}: output_path must end in .mp3")
            if path in seen:
                raise ValueError(f"two lines share the output_path {path}")
            seen.add(path)
            lines.append({"id": line_id, "text": text, "output_path": path})
        return lines

    @classmethod
    def _sample_text(cls, lines: list[dict[str, str]]) -> str:
        """What the candidates speak while the voice is designed: the start of the script, in the
        video's own language. ElevenLabs takes 100 to 1,000 characters and counts every one, and
        the recording itself is not used, so the sample ends with the first sentence that takes
        it past 100. A shorter script is said again until it is long enough."""
        script = " ".join(line["text"] for line in lines)
        text = script
        while len(text) < cls._SAMPLE_MIN:
            text = f"{text} {script}"
        ends = [found.end() for found in re.finditer(r"[.!?…](?=\s|$)|[。！？]", text)
                if cls._SAMPLE_MIN <= found.end() <= cls._SAMPLE_AIM]
        if ends:
            return text[:ends[0]]
        if len(text) > cls._SAMPLE_AIM:  # no sentence ends there: the last whole word that fits
            cut = text[:cls._SAMPLE_AIM]
            space = cut.rfind(" ")
            return cut[:space] if space >= cls._SAMPLE_MIN else cut
        return text

    @staticmethod
    def _gender(value: Any) -> str | None:
        word = str(value or "").strip().lower()
        if word in {"woman", "female", "f", "she", "her"}:
            return "woman"
        if word in {"man", "male", "m", "he", "his"}:
            return "man"
        return None

    @staticmethod
    def _age_band(value: Any) -> str | None:
        """young, middle or old from "early 30s", "mid forties", "60s", "52" or the words."""
        word = str(value or "").strip().lower()
        found = re.search(r"(\d{2})(s)?", word)
        years, decade = (int(found.group(1)), bool(found.group(2))) if found else (None, False)
        if years is None:
            for tens, name in enumerate(("twent", "thirt", "fort", "fift", "sixt", "sevent", "eight", "ninet"), start=2):
                if re.search(rf"\b{name}(y|ies)\b", word):
                    years, decade = tens * 10, True
                    break
        if years is not None:  # a number says more than a word beside it ("45 years old")
            if decade:  # "late 30s" is 38
                years += 8 if "late" in word else 5 if "mid" in word else 2 if "early" in word else 4
            return "young" if years < 35 else "middle" if years <= 55 else "old"
        for band, names in (("young", ("young", "teen")), ("middle", ("middle",)), ("old", ("old", "senior", "elder"))):
            if any(name in word for name in names):
                return band
        return None

    @classmethod
    def _stock_voice(cls, gender: str, band: str, description: str, project: str) -> str:
        if re.search(r"\b(british|england|english accent|london|uk)\b", description, re.I):
            return cls._STOCK_BRITISH[gender]
        options = cls._STOCK_VOICES[(gender, band)]
        return options[zlib.crc32(project.encode("utf-8")) % len(options)]

    @staticmethod
    def _project_dir(inputs: dict[str, Any], lines: list[dict[str, str]]) -> Path:
        """Which video this is. Inside a job the desk says so and nothing in the call can say
        otherwise: a video's voice is found, used and replaced by its project's name."""
        given = os.environ.get("OPENMONTAGE_PROJECT_DIR") or inputs.get("project_dir")
        if given:
            return Path(str(given))
        first = Path(lines[0]["output_path"]).resolve()
        for parent in first.parents:
            if parent.name == "assets":
                return parent.parent
        return first.parent

    def _read_record(self, project: Path) -> dict[str, Any] | None:
        try:
            record = json.loads((project / self._RECORD).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return record if isinstance(record, dict) and record.get("kind") in {"saved", "stock"} else None

    def _write_record(self, project: Path, record: dict[str, Any]) -> None:
        path = project / self._RECORD
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=1), encoding="utf-8")

    # ---- the price before the call --------------------------------------------------------

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        """A first call (it brings a voice description): the design's sample text plus the lines.
        A later call: the lines alone. The stock voice: fal's price a character."""
        try:
            lines = self._lines(inputs)
        except ValueError:
            return 0.0
        characters = sum(len(line["text"]) for line in lines)
        model = inputs.get("model") or self.DEFAULT_MODEL
        if model == self.STOCK:
            return round(characters * self._STOCK_USD_PER_CHARACTER, 4)
        units = math.ceil(characters * self.MODELS.get(model, self.MODELS[self.DEFAULT_MODEL])[1])
        if str(inputs.get("voice_description") or "").strip():
            units += len(self._sample_text(lines))
        return round(self._usd(units), 4)

    # ---- the call ----------------------------------------------------------------------------

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.time()
        try:
            lines = self._lines(inputs)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        model = str(inputs.get("model") or self.DEFAULT_MODEL)
        if model != self.STOCK and model not in self.MODELS:
            return ToolResult(success=False, error=f"model must be one of: {', '.join(self.MODELS)}, {self.STOCK}")
        project = self._project_dir(inputs, lines)
        spend = _Spend()
        try:
            record = None if inputs.get("new_voice") else self._record_or_saved_voice(project)
            if record:
                outcome = self._speak_again(record, lines, spend)
            else:
                outcome = self._first_call(inputs, lines, model, project, spend)
        except _Failed as exc:
            return ToolResult(success=False, error=str(exc), cost_usd=self._total(spend) or None,
                              data=self._money(spend), duration_seconds=round(time.time() - started, 2))
        data = {
            "provider": "elevenlabs" if outcome["kind"] == "saved" else "fal.ai",
            "model": outcome["model"],
            "voice": {key: outcome[key] for key in ("kind", "reused", "voice_id", "stock_voice") if outcome.get(key) is not None},
            "lines": [{"id": line["id"], "output": line["output_path"]} for line in lines],
            "outputs": [line["output_path"] for line in lines],
            **self._money(spend),
        }
        if outcome.get("fallback"):
            data["fallback"] = outcome["fallback"]
            data["note"] = (f"The lines are spoken by the stock voice {outcome['stock_voice']}, not by a designed "
                            f"voice: {outcome['fallback']}. Later calls for this video keep to that voice.")
            # For the operator (the job's warnings), never for the client.
            data["operator_note"] = f"voiceover_tts used the stock voice {outcome['stock_voice']}: {outcome['fallback']}"
        if outcome.get("made_room"):
            data["made_room"] = outcome["made_room"]
        return ToolResult(success=True, data=data, artifacts=[line["output_path"] for line in lines],
                          cost_usd=round(self._total(spend), 4), duration_seconds=round(time.time() - started, 2),
                          model=outcome["model"])

    def _total(self, spend: _Spend) -> float:
        return self._usd(spend.units) + spend.stock_usd

    def _money(self, spend: _Spend) -> dict[str, Any]:
        data: dict[str, Any] = {
            "elevenlabs_units": spend.units,
            "elevenlabs_request_ids": list(spend.eleven_ids),
            "costs": {"design_usd": round(self._usd(spend.design_units), 4),
                      "speech_usd": round(self._usd(spend.speech_units), 4),
                      "stock_usd": round(spend.stock_usd, 4)},
            # fal's ids go to the ledger only when fal's charge is the whole cost of the call:
            # settlement replaces a line's figure by what fal billed for its ids.
            "request_ids": list(spend.fal_ids) if not spend.units else [],
        }
        return data

    def _record_or_saved_voice(self, project: Path) -> dict[str, Any] | None:
        """The video's voice, from its record; a saved voice whose record is lost is still found by
        its name."""
        record = self._read_record(project)
        if record:
            return record
        key = voices.api_key()
        if not key:
            return None
        try:
            saved = voices.find(key, project.name)
        except voices.ElevenError:
            return None
        if not saved:
            return None
        return {"kind": "saved", "voice_id": str(saved.get("voice_id")), "model": self.DEFAULT_MODEL}

    @staticmethod
    def _drop_saved_voice(key: str, project: Path) -> None:
        try:
            for old in voices.ours(key):
                if voices.project_of(old) == project.name:
                    voices.remove(key, str(old.get("voice_id")))
        except voices.ElevenError:
            pass  # the provider's cleanup removes it with the video

    # -- a video's first call: make its voice ------------------------------------------------

    def _first_call(self, inputs: dict[str, Any], lines: list[dict[str, str]], model: str, project: Path,
                    spend: _Spend) -> dict[str, Any]:
        description = " ".join(str(inputs.get("voice_description") or "").split())[:1000]
        gender = self._gender(inputs.get("gender"))
        band = self._age_band(inputs.get("age"))
        if not description or not gender or not band:
            raise _Failed("This video has no voice on record. For a new video give voice_description, gender "
                          "(\"woman\" or \"man\") and age (as in the plan, e.g. \"early 30s\") together with every "
                          "line of the script. If this is a video made before designed voices, its plan names "
                          "the stock voice: speak the line with fal_elevenlabs_tts and that voice.")
        about = {"description": description, "gender": gender, "age": str(inputs.get("age")),
                 "language": str(inputs.get("language") or "en")}
        key = voices.api_key()
        if key and inputs.get("new_voice"):
            self._drop_saved_voice(key, project)  # the video's old voice goes before its new one comes
        if model == self.STOCK:
            return self._stock(lines, project, about, band, spend, reason=None)
        if not key:
            return self._stock(lines, project, about, band, spend, reason="this server has no ElevenLabs key")
        deadline = time.monotonic() + self._DESIGNED_SECONDS
        try:
            voice_id, made_room = self._design_and_save(key, about, lines, model, project, spend, deadline)
        except _Fallback as exc:
            return self._stock(lines, project, about, band, spend, reason=str(exc))
        try:
            self._speak_all(key, voice_id, self.MODELS[model][0], lines, spend, deadline)
        except (_Fallback, _VoiceGone) as exc:
            voices.remove(key, voice_id)  # a voice that could not be used is not kept
            reason = str(exc) or "the saved voice disappeared"
            return self._stock(lines, project, about, band, spend, reason=reason)
        self._write_record(project, {"kind": "saved", "voice_id": voice_id, "voice_name": voices.voice_name(project.name),
                                     "model": model, **about, "created_at": int(time.time())})
        return {"kind": "saved", "reused": False, "voice_id": voice_id, "model": model, "made_room": made_room}

    @staticmethod
    def _left(deadline: float, most: float) -> float:
        """How long one request may still take: what is left of the designed path's time."""
        return max(5.0, min(most, deadline - time.monotonic()))

    def _ask(self, deadline: float, most: float, method: str, path: str, key: str, **kwargs: Any) -> Any:
        """One ElevenLabs request of the designed path. "Too many requests at once" (another
        video's voice is being made) charges nothing and is waited out for as long as the path
        has time; any other refusal is the caller's."""
        while True:
            try:
                return voices.call(method, path, key, timeout=self._left(deadline, most), **kwargs)
            except voices.ElevenError as exc:
                if exc.status != 429 or time.monotonic() > deadline:
                    raise
                time.sleep(random.uniform(0.5, 1.5))

    @staticmethod
    def _who(gender: str, age: str) -> str:
        """The sentence that heads a description which does not say who is speaking."""
        age = " ".join(str(age).split())
        years = re.fullmatch(r"(\d{2})(?: ?years?(?: old)?)?", age, re.I)
        if years:
            return f"A {years.group(1)} year old {gender}."
        if re.fullmatch(r"(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)([- ]\w+)?", age, re.I):
            return f"A {age} year old {gender}."
        if re.search(r"\d0s\b|\b\w+(ties|teens)\b", age, re.I):
            return f"A {gender} in {'her' if gender == 'woman' else 'his'} {age}."
        return f"{'An' if age[:1].lower() in 'aeiou' else 'A'} {age} {gender}."

    def _design_and_save(self, key: str, about: dict[str, str], lines: list[dict[str, str]], model: str,
                         project: Path, spend: _Spend, deadline: float) -> tuple[str, str | None]:
        sample = self._sample_text(lines)
        characters = sum(len(line["text"]) for line in lines)
        needed = len(sample) + math.ceil(characters * self.MODELS[model][1])
        description = about["description"]
        # Gender and age head the description when it does not say who is speaking, or says less
        # than ElevenLabs takes (20 characters).
        if len(description) < 20 or not re.search(r"\b(wo)?man\b|\b(fe)?male\b", description, re.I):
            description = f"{self._who(about['gender'], about['age'])} {description}"[:1000]
        try:
            state = self._ask(deadline, 30, "GET", "/v1/user/subscription", key).json()
            left = voices.room(state if isinstance(state, dict) else {})
            if left["creations"] is not None and left["creations"] <= 0:
                raise _Fallback("this month's voice creations on the ElevenLabs account are used up")
            if left["characters"] is not None and left["characters"] < needed:
                raise _Fallback(f"the ElevenLabs account has too few characters left ({left['characters']}, "
                                f"this script needs about {needed})")
            made_room = None
            if left["slots"] is not None and left["slots"] <= 0:
                removed = voices.make_room(key)
                if not removed:
                    raise _Fallback("no voice slot is free and none of our kept voices is old enough to remove")
                made_room = voices.project_of(removed)
            design = self._ask(deadline, 60, "POST", "/v1/text-to-voice/design", key,
                               params={"output_format": self._FORMAT},
                               json={"voice_description": description, "model_id": self._DESIGN_MODEL, "text": sample})
            spend.design_units += len(sample)
            previews = design.json().get("previews") or []
            if not previews or not previews[0].get("generated_voice_id"):
                raise _Fallback("the voice design returned no candidate")
            body = {"voice_name": voices.voice_name(project.name), "voice_description": description,
                    "generated_voice_id": previews[0]["generated_voice_id"],
                    "labels": {"om_instance": voices.prefix(), "om_project": project.name,
                               "om_created": str(int(time.time()))}}
            try:
                saved = self._ask(deadline, 30, "POST", "/v1/text-to-voice", key, json=body)
            except voices.ElevenError as exc:
                # Another video may have taken the last slot while this voice was designed. Only
                # then is an older voice of ours removed, once; any other refusal removes nothing.
                if not self._no_slot(exc, key):
                    raise
                removed = voices.make_room(key)
                if not removed:
                    raise
                made_room = voices.project_of(removed)
                saved = self._ask(deadline, 30, "POST", "/v1/text-to-voice", key, json=body)
            voice_id = str(saved.json().get("voice_id") or "")
            if not voice_id:
                raise _Fallback("the voice was saved without an id")
            return voice_id, made_room
        except voices.ElevenError as exc:
            raise _Fallback(str(exc)) from None

    @staticmethod
    def _no_slot(refusal: voices.ElevenError, key: str) -> bool:
        """Was a save refused because every slot is taken? ElevenLabs answers that with a plain
        400 "You have reached your maximum amount of custom voices (3 / 3)" (seen 2026-11-11);
        the account's own count of saved voices, which follows at once, says the same."""
        if "maximum amount of custom voices" in str(refusal).lower():
            return True
        slots = voices.room(voices.account(key))["slots"]
        return slots is not None and slots <= 0

    # -- speaking ------------------------------------------------------------------------------

    def _speak_all(self, key: str, voice_id: str, model_id: str, lines: list[dict[str, str]], spend: _Spend,
                   deadline: float) -> None:
        factor = next((f for m, f in self.MODELS.values() if m == model_id), 0.23)
        # Once one line cannot be spoken the call takes another path: the lines still waiting are
        # not spoken (and not paid for) in a voice that will not be used.
        stop = threading.Event()
        audio: dict[str, bytes] = {}

        def one(line: dict[str, str]) -> None:
            try:
                speak(line)
            except BaseException:
                stop.set()
                raise

        def speak(line: dict[str, str]) -> None:
            tries = 0
            while True:
                if stop.is_set():
                    return
                if time.monotonic() > deadline:
                    raise _Fallback("ElevenLabs did not speak the lines in time")
                try:
                    response = voices.call("POST", f"/v1/text-to-speech/{voice_id}", key,
                                           timeout=self._left(deadline, 60), params={"output_format": self._FORMAT},
                                           json={"text": line["text"], "model_id": model_id})
                except voices.ElevenError as exc:
                    if exc.status == 404 or exc.code in {"voice_not_found", "voice_does_not_exist"}:
                        raise _VoiceGone() from None
                    if exc.status == 429:  # too many requests at once: nothing was charged, wait and try again
                        time.sleep(random.uniform(0.5, 1.5))
                        continue
                    tries += 1
                    if (exc.status is None or exc.status >= 500) and tries <= 2:
                        time.sleep(1.0)
                        continue
                    raise _Fallback(f"line {line['id']}: {exc}") from None
                try:
                    units = int(response.headers.get("character-cost"))
                except (TypeError, ValueError):
                    units = math.ceil(len(line["text"]) * factor)
                spend.speech(units, response.headers.get("request-id"))  # charged, whatever follows
                if not response.content:
                    raise _Fallback(f"line {line['id']}: ElevenLabs returned no audio")
                audio[line["output_path"]] = response.content
                return

        with ThreadPoolExecutor(max_workers=self._AT_ONCE) as pool:
            list(pool.map(one, lines))
        # Only a whole voiceover reaches the project: a call that failed has changed no file.
        for target, content in audio.items():
            path = Path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def _speak_again(self, record: dict[str, Any], lines: list[dict[str, str]], spend: _Spend) -> dict[str, Any]:
        """A later call: the given lines in the voice the video already has."""
        if record["kind"] == "stock":
            self._stock_lines(str(record.get("stock_voice")), lines, spend)
            return {"kind": "stock", "reused": True, "stock_voice": record.get("stock_voice"), "model": self.STOCK}
        key = voices.api_key()
        if not key:
            raise _Failed("This video's voice is kept on ElevenLabs and this server has no ElevenLabs key now. "
                          "Leave the spoken words as they are.")
        model = str(record.get("model") or "")
        if model not in self.MODELS:
            model = self.DEFAULT_MODEL
        try:
            self._speak_all(key, str(record.get("voice_id")), self.MODELS[model][0], lines, spend,
                            time.monotonic() + self._DESIGNED_SECONDS)
        except _VoiceGone:
            raise _Failed(GONE) from None
        except _Fallback as exc:
            raise _Failed(f"The video's voice could not speak just now ({exc}). No other voice was used; "
                          "try once more, or leave the spoken words as they are.") from None
        return {"kind": "saved", "reused": True, "voice_id": record.get("voice_id"), "model": model}

    # -- the stock voice -----------------------------------------------------------------------

    def _stock(self, lines: list[dict[str, str]], project: Path, about: dict[str, str], band: str, spend: _Spend,
               reason: str | None) -> dict[str, Any]:
        name = self._stock_voice(about["gender"], band, about["description"], project.name)
        self._stock_lines(name, lines, spend)
        self._write_record(project, {"kind": "stock", "stock_voice": name, "model": self.STOCK, **about,
                                     "created_at": int(time.time()), **({"fallback": reason} if reason else {})})
        return {"kind": "stock", "reused": False, "stock_voice": name, "model": self.STOCK, "fallback": reason}

    def _stock_lines(self, name: str, lines: list[dict[str, str]], spend: _Spend) -> None:
        from tools.audio.fal_elevenlabs_tts import FalElevenLabsTTS

        tool = FalElevenLabsTTS()
        stop = threading.Event()
        # Each line is spoken into a file beside its place and moved there once every line is
        # spoken: a call that failed has changed no file.
        parts = {line["output_path"]: Path(line["output_path"]).with_name(f".{Path(line['output_path']).stem}.part.mp3")
                 for line in lines}

        def one(line: dict[str, str]) -> None:
            if stop.is_set():
                return
            result = tool.execute({"text": line["text"], "voice": name, "model_id": self._STOCK_MODEL,
                                   "output_path": str(parts[line["output_path"]])})
            if not result.success:
                stop.set()
                raise _Failed(f"The stock voice could not speak line {line['id']}: {result.error}")
            data = result.data if isinstance(result.data, dict) else {}
            cost = result.cost_usd if result.cost_usd is not None else len(line["text"]) * self._STOCK_USD_PER_CHARACTER
            spend.stock(float(cost), [str(r) for r in (data.get("request_ids") or []) if r])

        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(one, lines))
            for target, part in parts.items():
                os.replace(part, target)
        finally:
            for part in parts.values():
                part.unlink(missing_ok=True)
