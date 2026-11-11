"""What the director is doing, in plain words, for the client's live view.

A client showing a video being made has so far had only the director's own sentences: a dozen
lines in nine minutes, with nothing for minutes at a time, while the run made thirty tool calls
and a dozen generation calls. This module turns those into "activity" entries of the job's chat
stream (the journal the polling endpoint pages through):

    {"kind": "activity", "id": "t-4", "state": "started", "code": "plan", "label": "Writing the plan"}
    {"kind": "activity", "id": "t-4", "state": "done", "code": "plan", "label": "Writing the plan",
     "seconds": 23}

One step has one id. It is written when it starts, again when its label gets more exact, and when
it ends: `done`, `failed`, or `gone` for a step that turned out to be nothing worth keeping (a
second of thinking). A `started` entry of a step that began earlier carries `age`, the seconds it
has already run.

Two sources feed it: the session's own tool calls (with partial messages on, from the moment the
model starts writing the call), and the paid tool desk's ledger, where every generation call of the
background batch has its start and its end.

A label is chosen from the fixed lists below by matching the call. Nothing is copied from the call
into a label except a web host and a take's letter: no tool, model or provider name, no file or
folder, no command and no money. A call that matches nothing is "Working on the video". Nothing
here may fail a job: every entry point swallows its own errors.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

GENERIC = ("work", "Working on the video")
SETUP = ("setup", "Getting ready")
THINKING = ("thinking", "Thinking")
# Thinking shorter than this is dropped again instead of being kept in the list.
THINKING_KEEP_SECONDS = 10.0
# Steps that come in runs (reading the guide and its skills, timing each line's words) are one
# step: a new one joins the last while that is running or ended this recently.
GROUP_GAP_SECONDS = 20.0
# A run of generation calls is reported as ended once nothing has joined it for this long, so a
# run of one-second calls is one steady step and not a step that flickers.
GROUP_SETTLE_SECONDS = 3.0
# How much of a command has to be there before it is matched while it is still being written.
_PEEK_COMMAND = 160
_PEEK_LIMIT = 20000

# -- the session's own tool calls -----------------------------------------------------------------
# A shell command, first match wins. The kit's own commands are handled before this list.
_BASH_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (code, label, re.compile(pattern)) for code, label, pattern in (
        # A waiting loop. (The launch line prints "BURST EXIT" itself, so those words alone are no wait.)
        ("shoot_wait", "Waiting for the shoot to finish", r"\buntil\s+grep\b|\bwhile\s+!\s*grep\b"),
        ("shoot_check", "Checking on the shoot",
         r"\b(?:cat|tail|head|grep)\b[^|;&>\n]*\bburst\w*\.(?:out|log|txt)\b|\b(?:cat|tail|head)\b[^|;&>\n]*\.out\b"),
        ("shoot", "Starting the shoot", r"\bnohup\b|\bburst\w*"),
        ("report", "Writing the production notes", r"RUN_REPORT\.md"),
        ("post", "Writing the post text", r"post\.json"),
        ("plan", "Writing the plan", r"video_plan\.md"),
        ("edit", "Writing the shot list", r"edit\.json"),
        ("look", "Styling the look", r"src/look\b|tokens\.ts|look\.tsx|fc-list|fontsource|google-fonts"),
        ("scene", "Building a custom scene", r"src/scenes\b|scenes\.ts|\btsc\b"),
        ("final_check", "Checking frames of the final video", r"(?:ffmpeg|ffprobe)[^|;&]*exports/"),
        ("sound_check", "Checking the sound levels", r"volumedetect|ebur128|astats|loudnorm"),
        ("frames", "Checking frames of the footage", r"\bffmpeg\b|\bffprobe\b"),
        ("material", "Collecting real material from the site", r"\bcurl\b|\bwget\b|requests\.get|urlretrieve|urlopen|httpx\."),
        ("pictures", "Looking over the pictures", r"from PIL\b|Image\.open"),
        ("decisions", "Noting the decisions", r"decision|cost_log|checkpoint"),
        # One generation call made by hand, outside a batch: a tool is run, not just looked up.
        ("piece", "Making a piece of the video", r"\.execute\("),
        (*SETUP, r"provider_menu|tool_registry|registry\.discover|AGENT_GUIDE"),
        # Reading the studio's own manuals and what is installed.
        (*SETUP, r"skills/|pipeline_defs|os\.environ|\bwhich\b|^\s*cd\s+/app\b|/app/(?:tools|lib|remotion-composer|schemas)\b"
                 r"|^\s*(?:cd\s+\S+\s*(?:&&|;)\s*)?ls\b"),
    ))
# The kit's commands, in the words of a list ("Mixing the sound, checking the edit").
_KIT = re.compile(r"(?:\bkit|lib\.videokit)\s+(render|cover|sheet|mix|check|voice|look|init)\b")
_KIT_WORDS = {
    "render": ("render", "rendering the final video"),
    "cover": ("cover", "choosing the cover picture"),
    "sheet": ("sheet", "making a sheet of the key frames"),
    "mix": ("mix", "mixing the sound"),
    "check": ("check", "checking the edit"),
    "voice": ("words", "timing every spoken word"),
    "look": ("look", "setting up the look"),
    "init": ("look", "setting up the look"),
}
_DIRECT_RENDER = re.compile(r"\bremotion\s+render\b")

_IMAGE = re.compile(r"\.(?:png|jpe?g|webp|gif)$", re.I)
# A file being read, first match wins.
_READ_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (code, label, re.compile(pattern, re.I)) for code, label, pattern in (
        ("plan_read", "Reading the plan again", r"video_plan\.md$"),
        ("edit_read", "Reading the shot list again", r"edit\.json$"),
        ("timing", "Checking the timing", r"voice\.json$|resolved\.json$"),
        ("shoot_check", "Checking on the shoot", r"burst[^/\\]*\.(?:out|log|txt)$"),
        (*SETUP, r"AGENT_GUIDE|[/\\]skills[/\\]|[/\\]\.agents[/\\]|README|pipeline_defs|provider_menu|^/app/"),
    ))
# A file being written, first match wins.
_WRITE_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (code, label, re.compile(pattern, re.I)) for code, label, pattern in (
        ("plan", "Writing the plan", r"video_plan\.md$"),
        ("edit", "Writing the shot list", r"edit\.json$"),
        ("report", "Writing the production notes", r"RUN_REPORT\.md$"),
        ("post", "Writing the post text", r"post\.json$"),
        ("look", "Styling the look", r"[/\\]look[/\\]|tokens\.ts$"),
        ("scene", "Building a custom scene", r"[/\\]scenes[/\\]|scenes\.ts$"),
        ("shoot", "Preparing the shoot", r"burst"),
        ("decisions", "Noting the decisions", r"decision|cost_log|checkpoint"),
    ))


# -- the paid tool desk ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Desk:
    """A desk tool by its name. `group` joins a run of its calls into one step."""
    pattern: re.Pattern[str]
    code: str
    label: str
    group: bool = False


# First match wins.
_DESK_RULES: tuple[_Desk, ...] = (
    _Desk(re.compile(r"voiceover|tts", re.I), "voice", "Recording the voice"),
    _Desk(re.compile(r"transcrib|whisper", re.I), "words", "Timing every spoken word", group=True),
    _Desk(re.compile(r"music|lyria", re.I), "music", "Composing the music"),
    _Desk(re.compile(r"avatar|talking_head|lip", re.I), "take", "Filming the presenter"),
    _Desk(re.compile(r"compose|remotion|render", re.I), "render", "Rendering the final video"),
    _Desk(re.compile(r"image", re.I), "picture", "Making a picture"),
    _Desk(re.compile(r"video", re.I), "clip", "Animating a scene"),
    _Desk(re.compile(r"mixer", re.I), "mix", "Mixing the sound"),
)
_DESK_GENERIC = ("piece", "Making a piece of the video")
_TAKE = re.compile(r"take[_-]?([A-Za-z0-9]{1,2})(?:[_.-]|$)")
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,60}[a-z0-9])?")


def _sentence(words: list[str]) -> str:
    text = ", ".join(words[:2])
    return text[:1].upper() + text[1:]


def describe_command(command: str) -> tuple[str, str]:
    """(code, label) for a shell command."""
    kit = list(dict.fromkeys(_KIT.findall(command)))
    if "render" in kit or _DIRECT_RENDER.search(command):
        return "render", "Rendering the final video"
    if kit:
        return _KIT_WORDS[kit[0]][0], _sentence([_KIT_WORDS[name][1] for name in kit])
    for code, label, pattern in _BASH_RULES:
        if pattern.search(command):
            return code, label
    return GENERIC


def _picture(path: str) -> tuple[str, str]:
    folder = path.replace("\\", "/").lower()
    name = folder.rsplit("/", 1)[-1]
    if "cover" in name:
        return "cover_look", "Looking at the cover picture"
    if "sheet" in name:
        return "sheet_look", "Looking at the sheet of key frames"
    if "/refs/" in folder:
        return "pictures", "Looking over the pictures"
    if "portrait" in name:
        return "portrait_look", "Looking at the presenter's portrait"
    return "frames", "Looking at frames of the video"


def _host(url: str) -> str | None:
    try:
        host = (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
    except ValueError:
        return None
    host = host[4:] if host.startswith("www.") else host
    return host if "." in host and _HOST.fullmatch(host) else None


def describe_tool(name: str, tool_input: Any) -> tuple[str, str]:
    """(code, label) for one of the session's tool calls. Pure: the same call, the same words."""
    data = tool_input if isinstance(tool_input, dict) else {}
    if name == "Bash":
        return describe_command(str(data.get("command") or ""))
    if name in ("Read", "Write", "Edit"):
        path = str(data.get("file_path") or "")
        if not path:
            return GENERIC
        if name == "Read" and _IMAGE.search(path):
            return _picture(path)
        for code, label, pattern in (_READ_RULES if name == "Read" else _WRITE_RULES):
            if pattern.search(path):
                return code, label
        return ("checking", "Checking the work") if name == "Read" else GENERIC
    if name == "WebFetch":
        host = _host(str(data.get("url") or ""))
        return "research", f"Reading {host}" if host else "Reading a web page"
    if name == "WebSearch":
        return "search", "Searching the web"
    if name in ("Task", "Agent"):
        if str(data.get("subagent_type") or "").lower() == "composer":
            return "helper", "A second editor is building a custom scene"
        return "helper", "An assistant is checking the footage"
    if name == "Skill":
        return SETUP
    return GENERIC


def describe_desk(tool: str, output_path: Any = None) -> tuple[str, str, bool]:
    """(code, label, grouped) for one call at the paid tool desk."""
    name = Path(str(output_path)).name if output_path else ""
    for rule in _DESK_RULES:
        if rule.pattern.search(tool or ""):
            if rule.code == "take":
                match = _TAKE.search(name)
                if match:
                    return rule.code, f"Filming take {match.group(1).upper()}", False
            if rule.code == "picture" and "portrait" in name.lower():
                return "portrait", "Taking the presenter's portrait", False
            return rule.code, rule.label, rule.group
    return (*_DESK_GENERIC, False)


def _json_text(raw: str) -> str:
    """A JSON string's content as text, also when it is cut off mid-way."""
    raw = re.sub(r"\\u[0-9a-fA-F]{0,3}$", "", raw)
    if raw.endswith("\\") and not raw.endswith("\\\\"):
        raw = raw[:-1]
    try:
        return json.loads('"' + raw + '"')
    except ValueError:
        return raw.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")


_PEEK_FIELDS = {name: re.compile('"' + name + r'"\s*:\s*"((?:[^"\\]|\\.)*)"') for name in ("file_path", "url", "subagent_type")}
_PEEK_OPEN_COMMAND = re.compile(r'"command"\s*:\s*"((?:[^"\\]|\\.)*)')


def peek_input(name: str, written: str) -> dict[str, str] | None:
    """What a tool call's input says so far, while the model is still writing it: enough to name
    the step early, or None when there is not enough yet."""
    if name == "Bash":
        match = _PEEK_OPEN_COMMAND.search(written)
        if not match or len(match.group(1)) < _PEEK_COMMAND:
            return None
        return {"command": _json_text(match.group(1))}
    for field_name, pattern in _PEEK_FIELDS.items():
        match = pattern.search(written)
        if match:
            return {field_name: _json_text(match.group(1))}
    return None


# -- the feed -------------------------------------------------------------------------------------
@dataclass
class _Step:
    id: str
    code: str
    label: str
    started: datetime
    running: int = 1          # calls of this step still going (a group holds several)
    ended: datetime | None = None
    failed: bool = False
    reported: bool = False    # its end has been written


@dataclass
class _Call:
    """One tool call of the session and the step it shows as."""
    step: _Step
    name: str
    written: str = ""         # its input as far as the model has written it
    exact: bool = False       # its label came from the whole input


def _stamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _quiet(method: Callable[..., None]) -> Callable[..., None]:
    def guarded(self: "ActivityFeed", *args: Any, **kwargs: Any) -> None:
        try:
            method(self, *args, **kwargs)
        except Exception:  # the live view is a nicety: it never costs a job anything
            log.exception("activity feed: %s failed", method.__name__)
    guarded.__name__ = method.__name__
    guarded.__doc__ = method.__doc__
    return guarded


@dataclass
class ActivityFeed:
    """Writes one job's activity entries into its journal."""

    journal_path: Path
    clock: Callable[[], datetime] = _utcnow
    _serial: int = 0
    _calls: dict[str, _Call] = field(default_factory=dict)        # tool call id -> the call
    _blocks: dict[tuple[str, int], str] = field(default_factory=dict)  # (message, block) -> tool call id
    _thinking: dict[tuple[str, int], _Step] = field(default_factory=dict)
    _group: _Step | None = None                                   # the session's current run of setup calls
    _desk: dict[str, _Step] = field(default_factory=dict)         # ledger call id -> its step
    _desk_groups: dict[str, _Step] = field(default_factory=dict)  # code -> the current run of grouped calls
    _ledger_seen: int = 0
    _message: str = ""

    def __post_init__(self) -> None:
        # A job whose server restarted keeps its journal: carry on after the ids already in it.
        try:
            with open(self.journal_path, encoding="utf-8") as handle:
                for line in handle:
                    if '"activity"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    match = re.fullmatch(r"[td]-(\d+)", str(entry.get("id") or ""))
                    if match:
                        self._serial = max(self._serial, int(match.group(1)))
        except OSError:
            pass

    def skip_ledger(self, entries: int) -> None:
        """The ledger entries a restarted job already had: their steps are in the journal."""
        self._ledger_seen = max(self._ledger_seen, int(entries))

    # -- writing ----------------------------------------------------------------------------------
    def _write(self, step: _Step, state: str, *, seconds: float | None = None, age: float | None = None) -> None:
        entry: dict[str, Any] = {"at": self.clock().isoformat(timespec="seconds"), "kind": "activity",
                                 "id": step.id, "state": state, "code": step.code, "label": step.label}
        if seconds is not None:
            entry["seconds"] = max(0, int(round(seconds)))
        if age is not None and age >= 1:
            entry["age"] = int(age)
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.journal_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _new(self, prefix: str, code: str, label: str, started: datetime | None = None) -> _Step:
        self._serial += 1
        return _Step(id=f"{prefix}-{self._serial}", code=code, label=label, started=started or self.clock())

    def _age(self, step: _Step) -> float:
        return (self.clock() - step.started).total_seconds()

    @staticmethod
    def _joinable(group: _Step | None, now: datetime) -> bool:
        return group is not None and bool(
            group.running or (group.ended and (now - group.ended).total_seconds() <= GROUP_GAP_SECONDS))

    def _join(self, group: _Step) -> None:
        group.running += 1
        group.ended = None
        if group.reported:
            group.reported = False
            self._write(group, "started", age=self._age(group))

    # -- the session's tool calls -----------------------------------------------------------------
    def _open(self, tool_id: str, name: str, code: str, label: str, *, exact: bool) -> None:
        """A tool call starts (or is first heard of). Setup calls join the current run of them."""
        now = self.clock()
        if code == SETUP[0] and self._joinable(self._group, now):
            assert self._group is not None
            self._join(self._group)
            self._calls[tool_id] = _Call(self._group, name, exact=exact)
            return
        step = self._new("t", code, label, now)
        self._calls[tool_id] = _Call(step, name, exact=exact)
        if code == SETUP[0]:
            self._group = step
        self._write(step, "started")

    def _rename(self, tool_id: str, code: str, label: str, *, exact: bool) -> None:
        """A call's input says more exactly what it is."""
        call = self._calls[tool_id]
        step = call.step
        call.exact = call.exact or exact
        if (code, label) == (step.code, step.label) or (not exact and (code, label) == GENERIC):
            return
        if step.running > 1:
            # One call of a run turned out to be something else: it gets a step of its own.
            step.running -= 1
            del self._calls[tool_id]
            self._open(tool_id, call.name, code, label, exact=call.exact)
            return
        if code == SETUP[0] and step is not self._group and self._joinable(self._group, self.clock()):
            # It belongs to the run of setup calls that is already showing.
            assert self._group is not None
            self._write(step, "gone")
            self._join(self._group)
            call.step = self._group
            return
        if step is self._group and code != SETUP[0]:
            self._group = None
        elif code == SETUP[0]:
            self._group = step
        step.code, step.label = code, label
        self._write(step, "started", age=self._age(step))

    @_quiet
    def tool_started(self, tool_id: str, name: str) -> None:
        """The model began writing a tool call (partial messages): its name is known, not its input."""
        if not tool_id or tool_id in self._calls:
            return
        code, label = describe_tool(name, {})
        self._open(tool_id, name, code, label, exact=False)

    @_quiet
    def tool_writing(self, tool_id: str, more: str) -> None:
        """More of a tool call's input arrived: name the step as soon as its input says what it is."""
        call = self._calls.get(tool_id)
        if call is None or call.exact or not more or len(call.written) > _PEEK_LIMIT:
            return
        call.written += more
        seen = peek_input(call.name, call.written)
        if seen:
            code, label = describe_tool(call.name, seen)
            if (code, label) != GENERIC:
                self._rename(tool_id, code, label, exact=False)

    @_quiet
    def tool_called(self, tool_id: str, name: str, tool_input: Any) -> None:
        """The whole tool call is known (with or without partial messages before it)."""
        if not tool_id:
            return
        code, label = describe_tool(name, tool_input)
        if tool_id in self._calls:
            call = self._calls[tool_id]
            call.written, call.name = "", name
            self._rename(tool_id, code, label, exact=True)
        else:
            self._open(tool_id, name, code, label, exact=True)

    @_quiet
    def tool_finished(self, tool_id: str) -> None:
        call = self._calls.pop(tool_id, None)
        if call is None:
            return
        step = call.step
        step.running = max(0, step.running - 1)
        if step.running:
            return
        step.ended, step.reported = self.clock(), True
        self._write(step, "done", seconds=(step.ended - step.started).total_seconds())

    # -- partial messages -------------------------------------------------------------------------
    @_quiet
    def stream_event(self, event: Any, parent_tool_use_id: str | None = None) -> None:
        """One raw streaming event of the model's answer (SDK StreamEvent.event)."""
        if parent_tool_use_id or not isinstance(event, dict):
            return  # a helper's own calls belong to the step of the call that started it
        kind = event.get("type")
        if kind == "message_start":
            self._message = str((event.get("message") or {}).get("id") or f"m{self._serial}")
            return
        key = (self._message, int(event.get("index") or 0))
        if kind == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use" and block.get("id"):
                self._blocks[key] = str(block["id"])
                self.tool_started(str(block["id"]), str(block.get("name") or ""))
            elif block.get("type") in ("thinking", "redacted_thinking"):
                step = self._new("t", *THINKING)
                self._thinking[key] = step
                self._write(step, "started")
        elif kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "input_json_delta" and key in self._blocks:
                self.tool_writing(self._blocks[key], str(delta.get("partial_json") or ""))
        elif kind == "content_block_stop":
            self._blocks.pop(key, None)
            step = self._thinking.pop(key, None)
            if step is not None:
                self._end_thinking(step)

    def _end_thinking(self, step: _Step) -> None:
        seconds = self._age(step)
        if seconds >= THINKING_KEEP_SECONDS:
            self._write(step, "done", seconds=seconds)
        else:
            self._write(step, "gone")

    @_quiet
    def settle(self) -> None:
        """A session is over: nothing of it is left running in the list."""
        for step in list(self._thinking.values()):
            self._end_thinking(step)
        self._thinking.clear()
        self._blocks.clear()
        for tool_id in list(self._calls):
            self.tool_finished(tool_id)

    # -- the paid tool desk -----------------------------------------------------------------------
    def _settle_groups(self, *, force: bool = False) -> None:
        """A run of generation calls that nothing has joined for a moment is reported as ended."""
        now = self.clock()
        for step in self._desk_groups.values():
            if step.running or step.reported or step.ended is None:
                continue
            if force or (now - step.ended).total_seconds() >= GROUP_SETTLE_SECONDS:
                step.reported = True
                self._write(step, "done", seconds=(step.ended - step.started).total_seconds())

    @_quiet
    def ledger(self, entries: list[dict[str, Any]]) -> None:
        """The job's tool ledger as it is now: every call's start and end becomes a step."""
        fresh, self._ledger_seen = entries[self._ledger_seen:], max(self._ledger_seen, len(entries))
        try:
            self._ledger_entries(fresh)
        finally:
            self._settle_groups()

    def _ledger_entries(self, fresh: list[dict[str, Any]]) -> None:
        for entry in fresh:
            event, call_id = entry.get("event"), str(entry.get("id") or "")
            at = _stamp(entry.get("at")) or self.clock()
            if event == "start" and call_id:
                code, label, grouped = describe_desk(str(entry.get("tool") or ""), entry.get("output_path"))
                group = self._desk_groups.get(code) if grouped else None
                if self._joinable(group, at):
                    assert group is not None
                    self._join(group)
                    self._desk[call_id] = group
                    continue
                step = self._new("d", code, label, at)
                self._desk[call_id] = step
                if grouped:
                    self._desk_groups[code] = step
                self._write(step, "started", age=self._age(step))
            elif event == "finish" and call_id in self._desk:
                step = self._desk.pop(call_id)
                step.running = max(0, step.running - 1)
                step.ended = at
                if self._desk_groups.get(step.code) is step:
                    continue  # a run of calls: reported once nothing has joined it for a moment
                step.failed = not entry.get("success")
                step.reported = True
                self._write(step, "failed" if step.failed else "done",
                            seconds=(at - step.started).total_seconds())

    @_quiet
    def close(self) -> None:
        """The job is over: a desk call that never reported its end is taken off the list."""
        self.settle()
        for step in {id(step): step for step in self._desk.values()}.values():
            if step.running:
                step.running, step.reported = 0, True
                self._write(step, "gone")
        self._desk.clear()
        self._settle_groups(force=True)
