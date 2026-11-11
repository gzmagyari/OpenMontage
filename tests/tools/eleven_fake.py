"""A stand-in for the ElevenLabs API that keeps the account's limits the way the real one does:
voices saved at once, voices created a month, characters, and what each call answers.

Shapes follow real calls of 2026-11-10 and 11: the subscription record's counters, the three
candidates of a design, `character-cost` and `request-id` on a speech answer and none on a
design, HTTP 429 with code `concurrent_limit_exceeded`, a voice's name, labels and
`created_at_unix`, the 400 a save gets when every slot is taken, and the 404 `voice_not_found`
of speech with a voice that was removed.
"""

from __future__ import annotations

import base64
import math
import threading
import time
from typing import Any
from urllib.parse import urlparse


class _Response:
    def __init__(self, status: int = 200, body: Any = None, content: bytes = b"", headers: dict | None = None):
        self.status_code = status
        self._body = body
        self.content = content
        self.headers = headers or {}
        self.text = "" if body is None else str(body)

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


def _error(status: int, code: str, message: str) -> _Response:
    return _Response(status, {"detail": {"type": "error", "code": code, "message": message}})


class FakeEleven:
    def __init__(self, *, slots: int = 3, creations: int = 54, characters: int = 10_000):
        self.voice_limit = slots
        self.max_add_edits = creations
        self.character_limit = characters
        self.character_count = 0
        self.add_edit_counter = 0
        self.voices: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.speech: list[tuple[str, str, str]] = []  # (voice id, model, text)
        self.design_error: _Response | None = None
        self.design_busy = 0  # how many designs are answered "too many requests at once" first
        # Every design that was answered: its description and the text its candidates speak. A
        # candidate's recording is the text itself, marked with the candidate it belongs to.
        self.designs: list[dict[str, str]] = []
        self.recordings = True  # False: candidates come without a recording
        self.save_errors: list[_Response] = []
        # Statuses a speech call answers before it works, taken one per call: 429, 500 ...
        self.speech_troubles: list[int] = []
        self.speech_always: int | None = None
        # Called once a design is answered: what another video does to the account meanwhile.
        self.after_design = None
        self.page_size: int | None = None
        self.most_at_once = 0
        self._at_once = 0
        self._serial = 0
        self._lock = threading.Lock()

    # -- helpers for tests -------------------------------------------------------------------
    def add_voice(self, name: str, *, age_seconds: float = 0, category: str = "generated", labels: dict | None = None) -> str:
        with self._lock:
            self._serial += 1
            voice_id = f"voice-{self._serial}"
        created = int(time.time() - age_seconds)
        self.voices[voice_id] = {"voice_id": voice_id, "name": name, "category": category,
                                 "labels": {"om_created": str(created), **(labels or {})},
                                 "created_at_unix": created}
        return voice_id

    def no_slot(self) -> _Response:
        """What a save is answered when every slot is taken (a real answer, 2026-11-11)."""
        return _error(400, "bad_request",
                      f"You have reached your maximum amount of custom voices ({self.voice_limit} / {self.voice_limit}). "
                      "You can upgrade your subscription to increase your custom voice limit.")

    def paths(self, method: str | None = None) -> list[str]:
        return [path for verb, path in self.calls if method is None or verb == method]

    def count(self, method: str, start: str) -> int:
        return sum(1 for verb, path in self.calls if verb == method and path.startswith(start))

    # -- the API -----------------------------------------------------------------------------
    def request(self, method: str, url: str, headers: dict | None = None, timeout: float | None = None,
                params: dict | None = None, json: dict | None = None, **_: Any) -> _Response:
        assert (headers or {}).get("xi-api-key") == "eleven-test-key", "every call carries the key"
        path = urlparse(url).path
        with self._lock:
            self.calls.append((method, path))
        if (method, path) == ("GET", "/v1/user/subscription"):
            return _Response(200, {
                "tier": "starter", "character_count": self.character_count, "character_limit": self.character_limit,
                "voice_limit": self.voice_limit, "voice_slots_used": len(self.voices),
                "max_voice_add_edits": self.max_add_edits, "voice_add_edit_counter": self.add_edit_counter})
        if (method, path) == ("GET", "/v2/voices"):
            return self._list(params or {})
        if (method, path) == ("POST", "/v1/text-to-voice/design"):
            return self._design(json or {})
        if (method, path) == ("POST", "/v1/text-to-voice"):
            return self._save(json or {})
        if method == "POST" and path.startswith("/v1/text-to-speech/"):
            return self._speak(path.rsplit("/", 1)[-1], json or {})
        if method == "DELETE" and path.startswith("/v1/voices/"):
            return self._delete(path.rsplit("/", 1)[-1])
        return _error(404, "not_found", f"no such route: {method} {path}")

    def _list(self, params: dict) -> _Response:
        every = sorted(self.voices.values(), key=lambda v: v["voice_id"])
        if not self.page_size:
            return _Response(200, {"voices": every, "has_more": False, "next_page_token": None})
        start = int(params.get("next_page_token") or 0)
        page = every[start:start + self.page_size]
        more = start + self.page_size < len(every)
        return _Response(200, {"voices": page, "has_more": more,
                               "next_page_token": str(start + self.page_size) if more else None})

    def _design(self, body: dict) -> _Response:
        if self.design_error is not None:
            return self.design_error
        if self.design_busy > 0:
            self.design_busy -= 1
            return _error(429, "concurrent_limit_exceeded", "Too many concurrent requests.")
        text = str(body.get("text") or "")
        if not 100 <= len(text) <= 1000:
            return _Response(422, {"detail": [{"type": "string_too_short", "msg": "String should have at least 100 characters"}]})
        assert body.get("model_id") == "eleven_ttv_v3" and body.get("voice_description")
        with self._lock:
            self._serial += 1
            serial = self._serial
            self.character_count += len(text)
            self.designs.append({"voice_description": str(body["voice_description"]), "text": text})
        if self.after_design is not None:
            self.after_design()

        def recording(candidate: int) -> str:
            if not self.recordings:
                return ""
            return base64.b64encode(f"REC[gen-{serial}-{candidate}]{text}".encode()).decode()

        return _Response(200, {"text": text, "previews": [
            {"generated_voice_id": f"gen-{serial}-{i}", "audio_base_64": recording(i), "media_type": "audio/mpeg",
             "duration_secs": 12.0} for i in range(3)]})

    def _save(self, body: dict) -> _Response:
        if self.save_errors:
            return self.save_errors.pop(0)
        if len(self.voices) >= self.voice_limit:
            return self.no_slot()
        if self.add_edit_counter >= self.max_add_edits:
            return _error(400, "voice_add_edit_limit_reached", "No voice operations left this month.")
        assert body.get("voice_name") and body.get("voice_description") and str(body.get("generated_voice_id")).startswith("gen-")
        voice_id = self.add_voice(str(body["voice_name"]), labels=dict(body.get("labels") or {}))
        self.voices[voice_id]["generated_voice_id"] = body["generated_voice_id"]
        self.voices[voice_id]["description"] = body["voice_description"]
        self.add_edit_counter += 1
        return _Response(200, {"voice_id": voice_id, "name": body["voice_name"], "category": "generated"})

    def _speak(self, voice_id: str, body: dict) -> _Response:
        with self._lock:
            self._at_once += 1
            self.most_at_once = max(self.most_at_once, self._at_once)
            trouble = self.speech_troubles.pop(0) if self.speech_troubles else self.speech_always
        try:
            time.sleep(0.01)
            if voice_id not in self.voices:
                return _error(404, "voice_not_found", "A voice with that id was not found.")
            if trouble == 429:
                return _error(429, "concurrent_limit_exceeded", "Too many concurrent requests.")
            if trouble:
                return _error(trouble, "internal_error", "Something went wrong.")
            text, model = str(body.get("text") or ""), str(body.get("model_id"))
            units = math.ceil(len(text) * (0.115 if model == "eleven_v4_turbo" else 0.23))
            with self._lock:
                self._serial += 1
                serial = self._serial
                self.character_count += units
                self.speech.append((voice_id, model, text))
            return _Response(200, None, content=f"MP3[{voice_id}]{text}".encode(),
                             headers={"character-cost": str(units), "request-id": f"req-{serial}"})
        finally:
            with self._lock:
                self._at_once -= 1

    def _delete(self, voice_id: str) -> _Response:
        if voice_id not in self.voices:
            return _error(404, "voice_not_found", "A voice with that id was not found.")
        del self.voices[voice_id]
        return _Response(200, {"status": "ok"})
