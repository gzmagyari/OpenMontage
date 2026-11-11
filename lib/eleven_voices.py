"""Voices this installation keeps on ElevenLabs: one per video, made by `voiceover_tts`.

A video's voice is designed from a description, saved, and kept for as long as the video itself
is kept, so a later revision can speak a changed line in the same voice. Two things use this
module: the tool (tools/audio/voiceover_tts.py), which makes and uses the voices, and the job
manager's cleanup (engine/jobs/manager.py), which removes a voice once its video is gone.

What the account limits (read from a real account and from eight calls at once, 2026-11-11):

- voices saved at once (``voice_limit``; 3 on the smallest plan). A new video that finds none
  free removes the oldest of OUR voices first (`make_room`);
- voices created a month (``max_voice_add_edits``; 54). Removing a voice does not give the
  creation back, so past the limit a video gets another kind of voice until the month resets;
- requests at once (3). One too many is answered with HTTP 429, code
  ``concurrent_limit_exceeded``, and no Retry-After.

Seen with real calls through the tool, 2026-11-11: the account's counts of saved voices and of
voices created follow a save at once (its character count follows some seconds later); a save
with every slot taken is answered 400, code ``bad_request``, "You have reached your maximum
amount of custom voices (3 / 3)", and does not count as a creation; speech with a voice that was
removed is answered 404, code ``voice_not_found``; a saved voice keeps the labels it was given,
is listed with ``created_at_unix``, and is found by searching for the start of its name.

Every voice we save is named ``<prefix>-<project id>`` and carries the same two in its labels.
The prefix belongs to this installation (`prefix`), so a local provider and a server sharing one
ElevenLabs account never remove each other's voices, and a voice without our prefix, the account
owner's own, is never touched.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Iterable

API = "https://api.elevenlabs.io"
# A voice younger than this may be speaking for its video right now: it is never removed to
# make room. A video's voice call takes well under a minute.
BUSY_SECONDS = 20 * 60
INSTANCE_ENV = "OPENMONTAGE_INSTANCE_ID"
_INSTANCE_FILE = "instance_id"
KEY_ENVS = ("ELEVENLABS_VOICE_API_KEY", "ELEVENLABS_API_KEY")


class ElevenError(Exception):
    """ElevenLabs refused or failed a call. `code` is its own error code where it gave one."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def api_key() -> str | None:
    """The key a video's voices are made and spoken with. Its own name comes first: under the
    general name the same key also switches on the older ElevenLabs tools (single lines, music,
    soundtracks) for every job, and they would spend the allowance the voices live on."""
    for name in KEY_ENVS:
        value = os.environ.get(name)
        if value:
            return value
    return None


def ensure_instance_id(data_dir: Path) -> str:
    """This installation's own id: taken from the environment when the operator set one, else
    read from the data folder, where it is written once. Put into the environment so that the
    tools the provider starts name their voices with it."""
    value = (os.environ.get(INSTANCE_ENV) or "").strip()
    if not value:
        path = Path(data_dir) / _INSTANCE_FILE
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if not value:
            value = os.urandom(4).hex()
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value + "\n", encoding="utf-8")
            except OSError:
                pass  # an id that lasts as long as this process is still an id of our own
    os.environ[INSTANCE_ENV] = value
    return value


def prefix() -> str:
    """The start of every voice name of this installation."""
    instance = "".join(ch for ch in (os.environ.get(INSTANCE_ENV) or "local") if ch.isalnum())[:16] or "local"
    return f"om-{instance}"


def voice_name(project_id: str) -> str:
    return f"{prefix()}-{project_id}"[:100]


def project_of(voice: dict[str, Any]) -> str | None:
    """The project a voice of ours was made for, or None when the voice is not ours."""
    name = str(voice.get("name") or "")
    start = prefix() + "-"
    if not name.startswith(start):
        return None
    return name[len(start):] or None


def created_at(voice: dict[str, Any]) -> float:
    """When a voice of ours was saved (our own label first: it is set on every save)."""
    labels = voice.get("labels")
    stamp = labels.get("om_created") if isinstance(labels, dict) else None
    for value in (stamp, voice.get("created_at_unix")):
        try:
            if value:
                return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _problem(response: Any) -> ElevenError:
    code = None
    try:
        detail = response.json().get("detail")
    except Exception:
        detail = None
    if isinstance(detail, dict):
        code = detail.get("code") or detail.get("status")
        message = str(detail.get("message") or detail)
    elif isinstance(detail, list) and detail:
        message = "; ".join(str(item.get("msg") if isinstance(item, dict) else item) for item in detail[:3])
    else:
        message = str(getattr(response, "text", ""))[:300] or "no details given"
    return ElevenError(f"ElevenLabs answered {response.status_code}: {message[:400]}",
                       status=response.status_code, code=str(code) if code else None)


def call(method: str, path: str, key: str, *, timeout: float = 60, **kwargs: Any) -> Any:
    """One request to ElevenLabs. Returns the response on 2xx; anything else is an ElevenError
    carrying ElevenLabs' own words. A network failure is an ElevenError too."""
    import requests

    headers = {"xi-api-key": key}
    if "json" in kwargs:
        headers["Content-Type"] = "application/json"
    try:
        response = requests.request(method, API + path, headers=headers, timeout=timeout, **kwargs)
    except Exception as exc:  # the network, a timeout, DNS
        raise ElevenError(f"ElevenLabs could not be reached: {type(exc).__name__}") from None
    if response.status_code >= 300:
        raise _problem(response)
    return response


def account(key: str) -> dict[str, Any]:
    """The account's own counters: characters, saved voices, voices created this month."""
    body = call("GET", "/v1/user/subscription", key, timeout=30).json()
    return body if isinstance(body, dict) else {}


def room(state: dict[str, Any]) -> dict[str, Any]:
    """What the account still has: slots, creations this month, characters. None = not told."""

    def left(limit: str, used: str) -> int | None:
        try:
            return int(state[limit]) - int(state[used])
        except (KeyError, TypeError, ValueError):
            return None

    return {"slots": left("voice_limit", "voice_slots_used"),
            "creations": left("max_voice_add_edits", "voice_add_edit_counter"),
            "characters": left("character_limit", "character_count")}


def ours(key: str) -> list[dict[str, Any]]:
    """Every saved voice of this installation, oldest first."""
    found: list[dict[str, Any]] = []
    token = None
    for _ in range(20):  # 100 a page; no plan saves 2,000 voices
        params: dict[str, Any] = {"page_size": 100, "search": prefix()}
        if token:
            params["next_page_token"] = token
        body = call("GET", "/v2/voices", key, timeout=30, params=params).json()
        for voice in body.get("voices") or []:
            if isinstance(voice, dict) and project_of(voice):
                found.append(voice)
        token = body.get("next_page_token")
        if not body.get("has_more") or not token:
            break
    return sorted(found, key=created_at)


def find(key: str, project_id: str) -> dict[str, Any] | None:
    """The saved voice of one project, if it still exists."""
    matches = [voice for voice in ours(key) if project_of(voice) == project_id]
    return matches[-1] if matches else None


def remove(key: str, voice_id: str) -> bool:
    """Remove a saved voice. True when it is gone afterwards (also when it already was)."""
    try:
        call("DELETE", f"/v1/voices/{voice_id}", key, timeout=60)
        return True
    except ElevenError as exc:
        return exc.status == 404 or exc.code in {"voice_not_found", "voice_does_not_exist"}


def make_room(key: str, *, now: float | None = None) -> dict[str, Any] | None:
    """Free one slot by removing the oldest voice of ours that is not busy. Returns the voice that
    was removed, or None when there is nothing we may remove."""
    now = time.time() if now is None else now
    for voice in ours(key):
        if now - created_at(voice) >= BUSY_SECONDS and remove(key, str(voice.get("voice_id"))):
            return voice
    return None


def sweep(key: str, kept_projects: Iterable[str]) -> list[str]:
    """Remove every saved voice of ours whose video is no longer kept. Returns the projects whose
    voices were removed. A voice that is busy (just saved) is left for a later round: its video
    may not be on record yet."""
    kept = set(kept_projects)
    now = time.time()
    removed = []
    for voice in ours(key):
        project = project_of(voice)
        if project in kept or now - created_at(voice) < BUSY_SECONDS:
            continue
        if remove(key, str(voice.get("voice_id"))):
            removed.append(str(project))
    return removed
