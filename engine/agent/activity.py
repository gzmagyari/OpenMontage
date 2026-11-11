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
# A shell command is named by what is in it. One command often does several things (writes the shot
# list, then checks it), so every act found in it counts, and the label names the first two in the
# order the command does them. An act has a weight: a main act leads the label; a minor one (a look
# at what is there) names the step only when no main act was found; a weak one (reading up) only
# when nothing else was.
_MAIN, _MINOR, _WEAK = 0, 1, 2


@dataclass(frozen=True)
class _Act:
    code: str
    label: str
    pattern: re.Pattern[str]
    weight: int = _MAIN
    # The pattern is a file's name and the act is writing that file. Only reading it is `read` (a
    # weak act) or nothing at all; changing it in place is `changed` where that reads truer.
    file: bool = False
    read: tuple[str, str] | None = None
    changed: str | None = None
    # The act only counts when this is in the command as well.
    needs: re.Pattern[str] | None = None


def _act(code: str, label: str, pattern: str, weight: int = _MAIN, *, needs: str | None = None, **more: Any) -> _Act:
    return _Act(code, label, re.compile(pattern), weight, needs=re.compile(needs) if needs else None, **more)


# The media tools being run (an option or, in a script, a quote follows), not merely named.
_PROBE = r"\b(?:ffmpeg|ffprobe)\b(?:\s+-|[\"'])"
# The acts a label never leaves out when the command has them.
_LEAD = ("shoot", "shoot_wait")
_ACTS: tuple[_Act, ...] = (
    # A waiting loop. (The launch line prints "BURST EXIT" itself, so those words alone are no wait.)
    _act("shoot_wait", "Waiting for the shoot to finish", r"\buntil\s+grep\b|\bwhile\s+!\s*grep\b"),
    # The batch of generation calls: launched in the background, or its script written.
    _act("shoot", "Starting the shoot", r"\bnohup\b"),
    _act("shoot", "Starting the shoot", r"\bburst\w*\.py\b", file=True),
    _act("report", "Writing the production notes", r"RUN_REPORT\.md", file=True),
    _act("post", "Writing the post text", r"post\.json", file=True),
    _act("plan", "Writing the plan", r"video_plan\.md", file=True, read=("plan_read", "Reading the plan again")),
    _act("edit", "Writing the shot list", r"edit\.json", file=True, changed="Adjusting the shot list",
         read=("edit_read", "Reading the shot list again")),
    _act("look", "Styling the look", r"src/look\b|tokens\.ts|look\.tsx", file=True, read=("look", "Styling the look")),
    _act("look", "Styling the look", r"fc-list|fontsource|google-fonts"),
    _act("scene", "Building a custom scene", r"src/scenes\b|scenes\.ts", file=True),
    _act("scene", "Building a custom scene", r"\btsc\b"),
    _act("final_check", "Checking frames of the final video", r"(?:ffmpeg|ffprobe)[^|;&]*exports/"),
    _act("takes_check", "Checking the takes", r"\btake[_-]?(?:[A-Za-z0-9]{1,2}\b|\$)", needs=_PROBE),
    _act("sound_check", "Checking the sound levels", r"volumedetect|ebur128|astats|loudnorm"),
    _act("material", "Collecting real material from the site",
         r"\bcurl\b|\bwget\b|requests\.get|urlretrieve|urlopen|httpx\.|sync_playwright|async_playwright|\.goto\("),
    # One generation call made by hand, outside a batch: a tool is run, not just looked up.
    _act("piece", "Making a piece of the video", r"\.execute\("),
    _act("shoot_check", "Checking on the shoot",
         r"\b(?:cat|tail|head|grep)\b[^|;&>\n]*\bburst\w*\.(?:out|log|txt)\b|\b(?:cat|tail|head)\b[^|;&>\n]*\.out\b", _MINOR),
    _act("frames", "Checking frames of the footage", _PROBE, _MINOR),
    _act("pictures", "Looking over the pictures", r"from PIL\b|Image\.open", _MINOR),
    _act("decisions", "Noting the decisions", r"decision_log|decisions?\.json|cost_log|checkpoint\w*\.json|write_checkpoint", _MINOR),
    _act(*SETUP, r"provider_menu|tool_registry|registry\.discover|AGENT_GUIDE|\blib\.(?:checkpoint|paths)\b", _WEAK),
    # Reading the studio's own manuals and what is installed.
    _act(*SETUP, r"skills/|pipeline_defs|os\.environ|\bwhich\b|^\s*cd\s+/app\b|/app/(?:tools|lib|remotion-composer|schemas)\b"
                 r"|^\s*(?:cd\s+\S+\s*(?:&&|;)\s*)?ls\b", _WEAK),
)
# The kit's commands. Each is a main act where it stands in the command.
_KIT = re.compile(r"(?:\bkit|lib\.videokit)\s+(render|cover|sheet|mix|check|voice|look|init)\b")
_KIT_WORDS = {
    "render": ("render", "Rendering the final video"),
    "cover": ("cover", "Choosing the cover picture"),
    "sheet": ("sheet", "Making a sheet of the key frames"),
    "mix": ("mix", "Mixing the sound"),
    "check": ("check", "Checking the edit"),
    "voice": ("words", "Timing every spoken word"),
    "look": ("look", "Setting up the look"),
    "init": ("look", "Setting up the look"),
}
_DIRECT_RENDER = re.compile(r"\bremotion\s+render\b")
# What a heredoc writes into a file is that file's text, not something the command does: a plan
# that mentions the post text is not writing the post text. A script it writes to run is code and
# stays, like a heredoc fed straight to an interpreter; the batch script's own calls are the shoot.
_WRITTEN_TEXT = re.compile(
    r"(\b(?:cat|tee)\b[^\n]*<<-?[ \t]*[\"']?(\w+)[\"']?[^\n]*)\n.*?(?:\n[ \t]*\2[ \t]*(?=\n|$)|\Z)", re.S)
_SCRIPT_FILE = re.compile(r"\.(?:py|sh|js|mjs)\b")
_BATCH_FILE = re.compile(r"\bburst\w*\.py\b")
# How a file's name is used where it stands in a command. Writing it takes evidence: a name that is
# only there (listed, printed, opened to read, passed to a tool) is a read.
_CD = re.compile(r"(?:^|[\s(])cd\s+\S*$")
_REDIRECT = re.compile(r">>?\s*\S*$|\btee\b(?:\s+-\w+)*\s+\S*$")
_SHELL_CHANGE = re.compile(r"(?:^|[\s(`$])(?:sed\s+(?:-[a-zA-Z]+\s+)*-i|perl\s+-\w*i\w*|mv|rm|touch|patch|truncate)\b")
_PY_OPEN = re.compile(r"open\(\s*f?[\"'][^\"']*$")
_PY_WRITE_MODE = re.compile(r"[^\"']*[\"']\s*,\s*[\"'][wax]")
_PY_SAVES = re.compile(r"[^\"']*[\"']\s*\)\s*\.write")
# A path put in a variable, in a script that writes something: the write goes through the variable.
_PY_ASSIGNED = re.compile(r"=[^=\n;]*[\"'][^\"'\n]*$")
_PY_WRITES = re.compile(r"[\"'][wa]b?\+?[\"']\s*[),]|\.write_text\(|\.write\(|json\.dump\(")
_MENTIONS_MOST = 40

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


def _listed(labels: list[str]) -> str:
    """Up to two labels as one: "Writing the shot list, checking the edit"."""
    first, *rest = labels[:2]
    return first + "".join(", " + label[:1].lower() + label[1:] for label in rest)


def _mention(command: str, start: int, end: int, script_writes: bool) -> str:
    """How the file named at command[start:end] is used there: written, changed in place, read, or
    only passed through (`cd` into its folder)."""
    cut = max(command.rfind(mark, 0, start) for mark in "\n;&|") + 1
    before, after = command[cut:start], command[end:end + 40]
    if _CD.search(before):
        return "passed"
    if _REDIRECT.search(before):
        return "written"
    if _SHELL_CHANGE.search(before):
        return "changed"
    if _PY_OPEN.search(before):
        return "changed" if _PY_WRITE_MODE.match(after) else "read"
    if _PY_SAVES.match(after) or (script_writes and _PY_ASSIGNED.search(before)):
        return "changed"
    return "read"


def _found(act: _Act, text: str, script_writes: bool) -> tuple[int, int, str, str] | None:
    """(weight, where, code, label) when the act is in the command."""
    if act.needs is not None and not act.needs.search(text):
        return None
    if not act.file:
        match = act.pattern.search(text)
        return (act.weight, match.start(), act.code, act.label) if match else None
    read_at = wrote_at = None
    written = False
    for count, match in enumerate(act.pattern.finditer(text)):
        if count >= _MENTIONS_MOST:
            break
        use = _mention(text, match.start(), match.end(), script_writes)
        if use == "read":
            read_at = match.start() if read_at is None else read_at
        elif use in ("written", "changed"):
            wrote_at = match.start() if wrote_at is None else wrote_at
            written = written or use == "written"
    if wrote_at is not None:
        return act.weight, wrote_at, act.code, act.label if written or not act.changed else act.changed
    if read_at is not None and act.read:
        return _WEAK, read_at, act.read[0], act.read[1]
    return None


def _acted(command: str) -> str:
    """The command without the text its heredocs only write into files."""
    def keep(match: re.Match[str]) -> str:
        opening = match.group(1)
        runs = _SCRIPT_FILE.search(opening) and not _BATCH_FILE.search(opening)
        return match.group(0) if runs else opening
    return _WRITTEN_TEXT.sub(keep, command)


def describe_command(command: str) -> tuple[str, str]:
    """(code, label) for a shell command."""
    text = _acted(command)
    kit = [(match.start(), match.group(1)) for match in _KIT.finditer(text)]
    if any(name == "render" for _, name in kit) or _DIRECT_RENDER.search(text):
        return "render", "Rendering the final video"
    script_writes = bool(_PY_WRITES.search(text))
    # (weight, where in the command, place in the list, code, label)
    found = [(_MAIN, where, -1, *_KIT_WORDS[name]) for where, name in kit]
    for place, act in enumerate(_ACTS):
        hit = _found(act, text, script_writes)
        if hit:
            found.append((hit[0], hit[1], place, hit[2], hit[3]))
    if not found:
        return GENERIC
    weight = min(item[0] for item in found)
    best = sorted(item for item in found if item[0] == weight)
    if weight == _WEAK:
        # Reading up on several things at once is getting ready, whatever else was opened with it.
        best = [item for item in best if item[3] == SETUP[0]] or best
    if weight != _MAIN:
        return best[0][3], best[0][4]
    if any(item[3] == "shoot" for item in best):
        # The batch's own generation calls are the shoot, not pieces made by hand.
        best = [item for item in best if item[3] != "piece"]
    labels = list(dict.fromkeys(item[4] for item in best))
    lead = next((item for item in best if item[3] in _LEAD), None)
    if lead is not None and lead[4] not in labels[:2]:
        return lead[3], _listed([labels[0], lead[4]])
    return (lead or best[0])[3], _listed(labels)


_REFS_NAME = re.compile(r"^refs?[_.-]|reference")
_PRESENTER_NAME = re.compile(r"portrait|presenter")


def _picture(path: str) -> tuple[str, str]:
    folder = path.replace("\\", "/").lower()
    name = folder.rsplit("/", 1)[-1]
    if "cover" in name:
        return "cover_look", "Looking at the cover picture"
    if "/refs/" in folder or _REFS_NAME.search(name):
        return "pictures", "Looking over the pictures"
    if "take" in name:
        return "takes_look", "Looking at the takes"
    if "sheet" in name:
        return "sheet_look", "Looking at the sheet of key frames"
    if _PRESENTER_NAME.search(name):
        return "portrait_look", "Looking at the presenter's portrait"
    if "/assets/images/" in folder:
        return "picture_look", "Looking at a picture"
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
                # An edit of the shot list that is already there changes it; a write makes it.
                return (code, "Adjusting the shot list") if (name, code) == ("Edit", "edit") else (code, label)
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
            if rule.code == "picture" and _PRESENTER_NAME.search(name.lower()):
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
