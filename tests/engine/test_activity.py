"""The live view: every step of a job in plain words, in the chat stream a client pages through."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

import engine.agent.session as agent_session
from engine.agent.activity import (
    GENERIC,
    ActivityFeed,
    describe_command,
    describe_desk,
    describe_tool,
    peek_input,
)

P = "/data/projects/job_0123456789abcdef"
# Nothing of the machinery may reach a client's screen: a tool, a model, a provider, a file or a
# folder, a command, money.
FORBIDDEN = re.compile(
    r"fal|eleven|h3|max|turbo|lyria|openai|gpt|claude|sonnet|remotion|ffmpeg|ffprobe|python|whisper|"
    r"\bkit\b|bash|json|\.md|\.py|\.mp[34]|\.png|\.jpg|/|\\|_|\$|usd|cost|budget|ledger|token|job", re.I)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture()
def feed(tmp_path):
    clock = Clock()
    made = ActivityFeed(tmp_path / "agent" / "journal.jsonl", clock=clock)
    made.tick = clock.tick  # type: ignore[attr-defined]
    return made


def entries(feed: ActivityFeed) -> list[dict]:
    path = Path(feed.journal_path)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


def last_by_id(feed: ActivityFeed) -> dict[str, dict]:
    return {entry["id"]: entry for entry in entries(feed)}


# -- the words --------------------------------------------------------------------------------------
# Real calls of video 719's director (2026-11-11), each with the label a client reads.
REAL_CALLS = [
    ("Read", {"file_path": "/app/AGENT_GUIDE.md"}, "Getting ready"),
    ("Bash", {"command": f"cd {P} && ls -la . composition work refs 2>&1 | head -60; cat work/provider_menu.json"}, "Getting ready"),
    ("WebFetch", {"url": "https://lumabrew.example", "prompt": "Describe everything this site says"}, "Reading lumabrew.example"),
    ("WebFetch", {"url": "https://www.shop.example.co.uk/products?id=3", "prompt": "x"}, "Reading shop.example.co.uk"),
    ("WebFetch", {"url": "not a url at all", "prompt": "x"}, "Reading a web page"),
    ("WebSearch", {"query": "lumabrew pricing"}, "Searching the web"),
    ("Bash", {"command": "ls pipeline_defs skills/pipelines/avatar-spokesperson .agents/skills/voice-design"}, "Getting ready"),
    ("Read", {"file_path": "/app/.agents/skills/voice-design/SKILL.md"}, "Getting ready"),
    ("Bash", {"command": f"cd {P}/refs && for n in portrait_before portrait_after; do curl -sS -o $n.jpg https://x/$n.jpg; done"},
     "Collecting real material from the site"),
    ("Bash", {"command": "cd /app; python - <<'E'\nfrom tools.tool_registry import registry\nregistry.discover()\nE"}, "Getting ready"),
    ("Bash", {"command": f"cd {P} && python -I - <<'E'\nfrom PIL import Image\nfs=['portrait_before']\nE"}, "Looking over the pictures"),
    ("Read", {"file_path": "/tmp/refs_sheet.jpg"}, "Looking at the sheet of key frames"),
    ("Read", {"file_path": f"{P}/refs/01_product_shot.png"}, "Looking over the pictures"),
    ("Bash", {"command": f"cd {P} && cat composition/src/look/look.tsx | head -400"}, "Styling the look"),
    ("Bash", {"command": f"cd {P} && mkdir -p artifacts assets/audio && cat > composition/edit.json <<'E'\n{{}}\nE"}, "Writing the shot list"),
    ("Bash", {"command": f"cd {P} && python - <<'E'\nimport json\np='composition/edit.json'\nE\nkit check --draft"}, "Checking the edit"),
    ("Bash", {"command": f"cd {P} && cat > artifacts/video_plan.md <<'E'\n# Plan\nE"}, "Writing the plan"),
    ("Bash", {"command": f"cd {P} && cat > artifacts/video_plan.md <<'E'\n# Plan\nE\nnohup python work/burst.py > work/burst.out 2>&1 &"},
     "Starting the shoot"),
    ("Bash", {"command": f"cd {P} && until grep -q 'BURST EXIT' work/burst.out; do sleep 2; done; tail -5 work/burst.out"},
     "Waiting for the shoot to finish"),
    ("Write", {"file_path": f"{P}/work/burst.py", "content": "import fal"}, "Preparing the shoot"),
    ("Bash", {"command": f"cd {P}/composition/src/look && python - <<'E'\nimport re\nt=open('tokens.ts').read()\nE"}, "Styling the look"),
    ("Read", {"file_path": f"{P}/assets/images/portrait.png"}, "Looking at the presenter's portrait"),
    ("Bash", {"command": f"cd {P} && python - <<'E'\nimport json\nd=[{{\"category\":\"render_runtime\"}}]\nopen('artifacts/decision_log.json','w')\nE"},
     "Noting the decisions"),
    ("Bash", {"command": f"cd {P} && mkdir -p work/qc && for f in take_A take_B; do ffmpeg -v error -y -ss 0.5 -i assets/video/$f.mp4 -frames:v 1 work/qc/$f.jpg; done"},
     "Checking frames of the footage"),
    ("Read", {"file_path": "/tmp/qc.jpg"}, "Looking at frames of the video"),
    ("Bash", {"command": f"cd {P} && for f in take_A take_B; do ffmpeg -hide_banner -i assets/video/$f.mp4 -af volumedetect -f null - 2>&1 | grep max_volume; done"},
     "Checking the sound levels"),
    ("Bash", {"command": f"cd {P} && kit mix 2>&1 | tail -8; kit check 2>&1 | tail -15; kit sheet 2>&1 | tail -5"},
     "Mixing the sound, checking the edit"),
    ("Read", {"file_path": f"{P}/work/kit/sheet.jpg"}, "Looking at the sheet of key frames"),
    ("Bash", {"command": f"cd {P} && python - <<'E'\np='composition/src/look/look.tsx'\nE\nkit check && kit render --name video"},
     "Rendering the final video"),
    ("Bash", {"command": f"cd {P} && for t in 0.1 9 13; do ffmpeg -v error -y -ss $t -i exports/video.mp4 -frames:v 1 /tmp/f$t.jpg; done"},
     "Checking frames of the final video"),
    ("Bash", {"command": f"cd {P} && kit cover --at 1.2"}, "Choosing the cover picture"),
    ("Read", {"file_path": f"{P}/exports/cover.jpg"}, "Looking at the cover picture"),
    ("Write", {"file_path": f"{P}/artifacts/post.json", "content": "{}"}, "Writing the post text"),
    ("Bash", {"command": f"cd {P} && cat > RUN_REPORT.md <<'E'\n## Summary\nE"}, "Writing the production notes"),
    ("Write", {"file_path": f"{P}/composition/src/scenes/PhoneDemo.tsx", "content": "x"}, "Building a custom scene"),
    ("Edit", {"file_path": f"{P}/composition/edit.json", "old_string": "a", "new_string": "b"}, "Writing the shot list"),
    ("Read", {"file_path": f"{P}/artifacts/video_plan.md"}, "Reading the plan again"),
    ("Read", {"file_path": f"{P}/work/kit/voice.json"}, "Checking the timing"),
    ("Read", {"file_path": f"{P}/work/burst.out"}, "Checking on the shoot"),
    ("Read", {"file_path": f"{P}/assets/audio/A_call.json"}, "Checking the work"),
    ("Task", {"subagent_type": "composer", "description": "Write PhoneDemo", "prompt": "..."}, "A second editor is building a custom scene"),
    ("Agent", {"subagent_type": "mechanic", "description": "probe", "prompt": "..."}, "An assistant is checking the footage"),
    ("Skill", {"skill": "voice-design"}, "Getting ready"),
    # The guide's own launch line and its wait (AGENT_GUIDE.md, Latency rule 5).
    ("Bash", {"command": f"cd {P} && nohup sh -c 'python work/burst.py; echo \"BURST EXIT $?\"' > work/burst.out 2>&1 &"},
     "Starting the shoot"),
    ("Bash", {"command": f"cd {P} && until grep -q \"BURST EXIT\" work/burst.out; do sleep 3; done; cat work/burst.out"},
     "Waiting for the shoot to finish"),
    ("Bash", {"command": f"cd {P} && sleep 15; cat work/burst1.out | tail -20; cat work/kit/voice.json"}, "Checking on the shoot"),
    # A tool run by hand is a piece of the video; looking a tool up is getting ready.
    ("Bash", {"command": "python -c \"from tools.tool_registry import registry; registry.discover(); "
                         "t=registry._tools['fal_avatar']; r=t.execute({'model':'h3max'}); print(r.success)\""},
     "Making a piece of the video"),
    ("Bash", {"command": "cd /app; grep -n 'trim_silence' tools/avatar/fal_avatar.py | head -30"}, "Getting ready"),
    ("Bash", {"command": "fc-list : family | sort -u | head -40"}, "Styling the look"),
    ("Bash", {"command": "echo $((2+2))"}, "Working on the video"),
    ("Read", {}, "Working on the video"),
    ("SomethingNew", {"anything": 1}, "Working on the video"),
]


@pytest.mark.parametrize("name,tool_input,label", REAL_CALLS, ids=[f"{n}-{i}" for i, (n, _, _) in enumerate(REAL_CALLS)])
def test_a_call_reads_as_what_it_does_for_the_video(name, tool_input, label):
    code, words = describe_tool(name, tool_input)
    assert words == label
    assert re.fullmatch(r"[a-z_]{2,24}", code)
    # The only thing a label takes from the call is a site's host.
    plain = words.replace("lumabrew.example", "").replace("shop.example.co.uk", "")
    assert not FORBIDDEN.search(plain), words


def test_the_kits_commands_read_as_a_list_and_the_render_wins():
    assert describe_command("kit voice") == ("words", "Timing every spoken word")
    assert describe_command("kit sheet --draft; kit check --draft") == ("sheet", "Making a sheet of the key frames, checking the edit")
    assert describe_command("python -m lib.videokit render --name v2") == ("render", "Rendering the final video")
    assert describe_command("cd composition && npx remotion render src/index.ts Main out.mp4")[0] == "render"
    assert describe_command("kit mix && kit check && kit render") == ("render", "Rendering the final video")
    # A sentence about the kit is not one of its commands.
    assert describe_command("echo 'the kit rendered fine'") == GENERIC


DESK_CALLS = [
    ("openai_image", "/p/assets/images/portrait.png", "Taking the presenter's portrait", False),
    ("openai_image", "/p/assets/images/broll_phone.png", "Making a picture", False),
    ("voiceover_tts", None, "Recording the voice", False),
    ("fal_elevenlabs_tts", "/p/assets/audio/A.mp3", "Recording the voice", False),
    ("transcriber", None, "Timing every spoken word", True),
    ("fal_lyria_music", "/p/assets/audio/music.mp3", "Composing the music", False),
    ("fal_elevenlabs_music", "/p/assets/audio/music.mp3", "Composing the music", False),
    ("fal_avatar", "/p/assets/video/take_B.mp4", "Filming take B", False),
    ("fal_avatar", "/p/assets/video/take_12.mp4", "Filming take 12", False),
    ("fal_avatar", "/p/assets/video/hook_presenter.mp4", "Filming the presenter", False),
    ("h3_max_video", "/p/assets/video/broll_phone.mp4", "Animating a scene", False),
    ("audio_mixer", "/p/assets/audio/mix.wav", "Mixing the sound", False),
    ("video_compose", "/p/exports/video.mp4", "Rendering the final video", False),
    ("a_tool_added_next_year", "/p/x.bin", "Making a piece of the video", False),
]


@pytest.mark.parametrize("tool,output,label,grouped", DESK_CALLS)
def test_a_generation_call_reads_as_what_it_makes(tool, output, label, grouped):
    code, words, group = describe_desk(tool, output)
    assert (words, group) == (label, grouped) and re.fullmatch(r"[a-z_]{2,24}", code)
    assert not FORBIDDEN.search(words), words


# -- the steps of a session -------------------------------------------------------------------------
def test_a_step_is_written_when_it_starts_and_when_it_ends(feed):
    feed.tool_called("toolu_1", "Bash", {"command": "kit render"})
    feed.tick(54.4)
    feed.tool_finished("toolu_1")
    first, last = entries(feed)
    assert first == {"at": "2026-11-11T12:00:00+00:00", "kind": "activity", "id": "t-1", "state": "started",
                     "code": "render", "label": "Rendering the final video"}
    assert last == {"at": "2026-11-11T12:00:54+00:00", "kind": "activity", "id": "t-1", "state": "done",
                    "code": "render", "label": "Rendering the final video", "seconds": 54}


def test_a_step_shows_from_the_moment_the_model_starts_writing_the_call(feed):
    """With partial messages the step is there while the call is being written: a plan takes the
    director twenty seconds to write, and the client watches a timer instead of nothing."""
    feed.stream_event({"type": "message_start", "message": {"id": "msg_1"}})
    feed.stream_event({"type": "content_block_start", "index": 1,
                       "content_block": {"type": "tool_use", "id": "toolu_9", "name": "Write", "input": {}}})
    assert entries(feed)[-1]["label"] == "Working on the video" and entries(feed)[-1]["state"] == "started"
    feed.tick(1)
    for piece in ('{"file_', 'path": "/data/projects/job_x/artifacts/video_', 'plan.md", "content": "# Plan\\n'):
        feed.stream_event({"type": "content_block_delta", "index": 1,
                           "delta": {"type": "input_json_delta", "partial_json": piece}})
    named = entries(feed)[-1]
    assert (named["id"], named["state"], named["label"], named["age"]) == ("t-1", "started", "Writing the plan", 1)
    feed.tick(22)
    feed.stream_event({"type": "content_block_stop", "index": 1})
    # The whole call arrives, then its result: the same step, nothing written twice.
    count = len(entries(feed))
    feed.tool_called("toolu_9", "Write", {"file_path": "/data/projects/job_x/artifacts/video_plan.md", "content": "# Plan"})
    assert len(entries(feed)) == count
    feed.tool_finished("toolu_9")
    done = entries(feed)[-1]
    assert (done["id"], done["state"], done["seconds"], done["label"]) == ("t-1", "done", 23, "Writing the plan")
    assert "age" not in done


def test_a_command_is_named_once_enough_of_it_is_written_and_again_when_it_is_whole(feed):
    feed.stream_event({"type": "message_start", "message": {"id": "msg_1"}})
    feed.stream_event({"type": "content_block_start", "index": 0,
                       "content_block": {"type": "tool_use", "id": "toolu_2", "name": "Bash", "input": {}}})
    start = "cd /data/projects/job_x && python - <<'E'\\nimport re\\np='composition/src/look/look.tsx'; s=open(p).read()\\n"
    feed.stream_event({"type": "content_block_delta", "index": 0,
                       "delta": {"type": "input_json_delta", "partial_json": '{"command": "' + start[:60]}})
    assert entries(feed)[-1]["label"] == "Working on the video", "sixty characters say too little"
    feed.stream_event({"type": "content_block_delta", "index": 0,
                       "delta": {"type": "input_json_delta", "partial_json": start[60:] + "s=s.replace('REC','')\\n" * 4}})
    assert entries(feed)[-1]["label"] == "Styling the look"
    # The end of the command is what it is really for.
    feed.tool_called("toolu_2", "Bash", {"command": "cd /data/projects/job_x && python fix_look.py && kit check && kit render"})
    assert entries(feed)[-1]["label"] == "Rendering the final video"
    feed.tool_finished("toolu_2")
    assert [entry["state"] for entry in entries(feed)] == ["started", "started", "started", "done"]
    assert {entry["id"] for entry in entries(feed)} == {"t-1"}


def test_peeking_at_a_half_written_call(feed):
    assert peek_input("Bash", '{"command": "kit ren') is None
    assert peek_input("Read", '{"file_path": "/app/AGENT_GUI') is None
    assert peek_input("Read", '{"file_path": "/app/AGENT_GUIDE.md"') == {"file_path": "/app/AGENT_GUIDE.md"}
    assert peek_input("WebFetch", '{"url": "https://example.com/a\\"b", "prompt": "Rea') == {"url": 'https://example.com/a"b'}
    # A command cut off in the middle of an escape still reads.
    cut_off = "x" * 200 + '\\n second line \\"quoted\\" \\'
    assert peek_input("Bash", '{"command": "' + cut_off)["command"].startswith("x" * 200 + '\n second line "quoted"')
    # A path quoted inside a file's content is not the call's path.
    assert peek_input("Write", '{"content": "see \\"file_path\\": \\"/etc/passwd\\" for') is None


def test_a_run_of_setup_calls_is_one_step(feed):
    """The director reads its guide and three skills before anything else: one "Getting ready",
    not four steps, and a call that turns out to be something else leaves the run."""
    feed.tool_called("a", "Read", {"file_path": "/app/AGENT_GUIDE.md"})
    feed.tick(2)
    feed.tool_called("b", "Read", {"file_path": "/app/.agents/skills/voice-design/SKILL.md"})
    feed.tool_called("c", "Bash", {"command": "ls pipeline_defs"})
    feed.tool_finished("a")
    feed.tick(3)
    feed.tool_finished("b")
    assert [entry["state"] for entry in entries(feed)] == ["started"], "still one step, still running"
    feed.tool_finished("c")
    assert entries(feed)[-1] == {"at": "2026-11-11T12:00:05+00:00", "kind": "activity", "id": "t-1", "state": "done",
                                 "code": "setup", "label": "Getting ready", "seconds": 5}
    # Ten seconds later another one: the same step picks up again.
    feed.tick(10)
    feed.tool_called("d", "Read", {"file_path": "/app/.agents/skills/fal-avatar/SKILL.md"})
    again = entries(feed)[-1]
    assert (again["id"], again["state"], again["age"]) == ("t-1", "started", 15)
    feed.tick(1)
    feed.tool_finished("d")
    assert entries(feed)[-1]["seconds"] == 16
    # A minute later it is a new step, and so is anything that is not setup.
    feed.tick(60)
    feed.tool_called("e", "Read", {"file_path": "/app/AGENT_GUIDE.md"})
    feed.tool_called("f", "Bash", {"command": "kit check"})
    assert [entry["id"] for entry in entries(feed)[-2:]] == ["t-2", "t-3"]


def test_a_call_first_taken_for_setup_gets_its_own_step_when_it_is_something_else(feed):
    feed.stream_event({"type": "message_start", "message": {"id": "m"}})
    for index, tool_id in enumerate(("r1", "r2")):
        feed.stream_event({"type": "content_block_start", "index": index,
                           "content_block": {"type": "tool_use", "id": tool_id, "name": "Skill", "input": {}}})
    assert len(entries(feed)) == 1 and entries(feed)[0]["label"] == "Getting ready"
    feed.tool_called("r2", "Bash", {"command": "kit render"})
    assert entries(feed)[-1]["label"] == "Rendering the final video" and entries(feed)[-1]["id"] == "t-2"
    feed.tool_finished("r1")
    assert entries(feed)[-1]["id"] == "t-1" and entries(feed)[-1]["state"] == "done"


def test_short_thinking_is_dropped_and_long_thinking_is_kept(feed):
    feed.stream_event({"type": "message_start", "message": {"id": "msg_1"}})
    feed.stream_event({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}})
    feed.tick(2)
    feed.stream_event({"type": "content_block_stop", "index": 0})
    feed.stream_event({"type": "message_start", "message": {"id": "msg_2"}})
    feed.stream_event({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}})
    feed.tick(34)
    feed.stream_event({"type": "content_block_stop", "index": 0})
    states = [(entry["id"], entry["state"], entry.get("seconds")) for entry in entries(feed)]
    assert states == [("t-1", "started", None), ("t-1", "gone", None), ("t-2", "started", None), ("t-2", "done", 34)]
    assert entries(feed)[0]["label"] == "Thinking"


def test_a_helpers_own_calls_are_not_steps(feed):
    feed.stream_event({"type": "content_block_start", "index": 0,
                       "content_block": {"type": "tool_use", "id": "inner", "name": "Bash", "input": {}}},
                      parent_tool_use_id="toolu_task")
    assert entries(feed) == []


def test_nothing_of_an_ended_session_stays_running(feed):
    feed.stream_event({"type": "message_start", "message": {"id": "msg_1"}})
    feed.stream_event({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}})
    feed.tool_called("x", "Bash", {"command": "kit sheet"})
    feed.tick(3)
    feed.settle()
    assert {entry["id"]: entry["state"] for entry in entries(feed)} == {"t-1": "gone", "t-2": "done"}


def test_a_broken_event_never_reaches_the_job(feed):
    feed.stream_event({"type": "content_block_start", "index": "not a number", "content_block": 7})
    feed.stream_event(None)
    feed.tool_writing("unknown", "{")
    feed.tool_finished("unknown")
    feed.ledger([{"event": "finish", "id": "never started"}, {"event": "start"}, "not an entry"])  # type: ignore[list-item]
    assert entries(feed) == []


# -- the generation calls of the background batch ---------------------------------------------------
def at(second: int) -> str:
    return (datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=second)).isoformat(timespec="seconds")


def test_every_generation_call_is_a_step_with_its_own_start_and_end(feed):
    """Video 719's batch, as its tool ledger recorded it: the journal shows it as one shell command."""
    ledger = [
        {"at": at(0), "event": "start", "id": "p", "tool": "openai_image", "output_path": "/p/assets/images/portrait.png"},
        {"at": at(0), "event": "start", "id": "v", "tool": "voiceover_tts", "output_path": None},
        {"at": at(0), "event": "start", "id": "m", "tool": "fal_lyria_music", "output_path": "/p/assets/audio/music.mp3"},
    ]
    feed.tick(3)   # the runner looks at the ledger three seconds later
    feed.ledger(ledger)
    assert [(e["id"], e["label"], e["age"]) for e in entries(feed)] == [
        ("d-1", "Taking the presenter's portrait", 3), ("d-2", "Recording the voice", 3), ("d-3", "Composing the music", 3)]
    ledger += [
        {"at": at(10), "event": "finish", "id": "p", "tool": "openai_image", "success": True, "cost_usd": 0.045},
        {"at": at(17), "event": "finish", "id": "v", "tool": "voiceover_tts", "success": True, "cost_usd": 0.07},
        {"at": at(18), "event": "start", "id": "w1", "tool": "transcriber"},
        {"at": at(20), "event": "finish", "id": "w1", "tool": "transcriber", "success": True},
        {"at": at(20), "event": "start", "id": "w2", "tool": "transcriber"},
        {"at": at(21), "event": "finish", "id": "w2", "tool": "transcriber", "success": True},
        {"at": at(22), "event": "start", "id": "w3", "tool": "transcriber"},
        {"at": at(25), "event": "finish", "id": "w3", "tool": "transcriber", "success": True},
        {"at": at(25), "event": "start", "id": "a", "tool": "fal_avatar", "output_path": "/p/assets/video/take_A.mp4"},
        {"at": at(25), "event": "start", "id": "b", "tool": "fal_avatar", "output_path": "/p/assets/video/take_B.mp4"},
        {"at": at(40), "event": "finish", "id": "m", "tool": "fal_lyria_music", "success": True, "cost_usd": 0.1},
        {"at": at(45), "event": "finish", "id": "a", "tool": "fal_avatar", "success": True, "cost_usd": 0.168},
        {"at": at(46), "event": "finish", "id": "b", "tool": "fal_avatar", "success": False, "cost_usd": 0},
        {"at": at(47), "event": "denied", "tool": "fal_avatar", "reason": "budget"},
    ]
    feed.tick(45)
    feed.ledger(ledger)
    final = last_by_id(feed)
    assert {step["label"]: (step["state"], step.get("seconds")) for step in final.values()} == {
        "Taking the presenter's portrait": ("done", 10), "Recording the voice": ("done", 17),
        "Composing the music": ("done", 40), "Timing every spoken word": ("done", 7),
        "Filming take A": ("done", 20), "Filming take B": ("failed", 21)}
    # The three word timings are one step, and a refused call is none.
    assert sum(1 for step in final.values() if step["label"] == "Timing every spoken word") == 1
    assert len(final) == 6
    # Reading the same ledger again writes nothing.
    count = len(entries(feed))
    feed.ledger(ledger)
    assert len(entries(feed)) == count
    for entry in entries(feed):
        assert not FORBIDDEN.search(entry["label"]), entry
        assert set(entry) <= {"at", "kind", "id", "state", "code", "label", "seconds", "age"}


def test_a_restarted_job_carries_on_after_what_its_journal_already_holds(feed):
    feed.tool_called("x", "Bash", {"command": "kit sheet"})
    feed.ledger([{"at": at(0), "event": "start", "id": "p", "tool": "openai_image", "output_path": "/p/portrait.png"}])
    again = ActivityFeed(feed.journal_path, clock=feed.clock)
    again.skip_ledger(1)
    again.ledger([{"at": at(0), "event": "start", "id": "p", "tool": "openai_image", "output_path": "/p/portrait.png"},
                  {"at": at(4), "event": "start", "id": "q", "tool": "fal_lyria_music"}])
    again.tool_called("y", "Bash", {"command": "kit render"})
    assert [entry["id"] for entry in entries(again)] == ["t-1", "d-2", "d-3", "t-4"]


def test_a_generation_call_that_never_reported_its_end_leaves_the_list_with_the_job(feed):
    feed.ledger([{"at": at(0), "event": "start", "id": "p", "tool": "fal_avatar", "output_path": "/p/take_A.mp4"}])
    feed.close()
    assert [(entry["id"], entry["state"]) for entry in entries(feed)] == [("d-1", "started"), ("d-1", "gone")]


# -- through a real session ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_session_feeds_the_live_view_and_keeps_partial_events_out_of_its_files(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    seen: dict = {}

    async def fake_query(*, prompt, options=None, **_):
        seen["partial"] = options.include_partial_messages
        yield SystemMessage(subtype="init", data={"session_id": "s1", "apiKeySource": "ANTHROPIC_API_KEY",
                                                  "model": options.model, "tools": ["Bash"]})
        yield StreamEvent(uuid="u1", session_id="s1", parent_tool_use_id=None,
                          event={"type": "message_start", "message": {"id": "msg_1"}})
        yield StreamEvent(uuid="u2", session_id="s1", parent_tool_use_id=None,
                          event={"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}})
        yield StreamEvent(uuid="u3", session_id="s1", parent_tool_use_id=None,
                          event={"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "input_json_delta", "partial_json": '{"command": "kit render"}'}})
        yield AssistantMessage(content=[TextBlock(text="Rendering now."),
                                        ToolUseBlock(id="toolu_1", name="Bash", input={"command": "kit render"})],
                               model=options.model)
        yield UserMessage(content=[ToolResultBlock(tool_use_id="toolu_1", content="ok", is_error=False)])
        # A helper's call is the helper's own business.
        yield AssistantMessage(content=[ToolUseBlock(id="toolu_inner", name="Bash", input={"command": "ffprobe x"})],
                               model=options.model, parent_tool_use_id="toolu_task")
        yield ResultMessage(subtype="success", duration_ms=10, duration_api_ms=10, is_error=False, num_turns=1,
                            session_id="s1", total_cost_usd=0.01, result="done")

    monkeypatch.setattr(agent_session, "sdk_query", fake_query)
    journal, transcript = tmp_path / "journal.jsonl", tmp_path / "transcript.jsonl"
    session = agent_session.AgentSession(repo_root=tmp_path, journal_path=journal, transcript_path=transcript,
                                         model="claude-haiku-5-5", max_turns=5, activity=ActivityFeed(journal))
    outcome = await session.run("go")
    assert outcome.succeeded and seen["partial"] is True
    lines = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    kinds = [line["kind"] for line in lines]
    assert kinds == ["session", "activity", "assistant", "tool", "activity", "tool_result", "activity", "tool", "result"]
    steps = [line for line in lines if line["kind"] == "activity"]
    assert [(step["id"], step["state"], step["label"]) for step in steps] == [
        ("t-1", "started", "Working on the video"), ("t-1", "started", "Rendering the final video"),
        ("t-1", "done", "Rendering the final video")]
    # The raw transcript holds whole messages only.
    assert "StreamEvent" not in transcript.read_text(encoding="utf-8")
    # Without a live view a session does not ask for partial messages at all.
    plain = agent_session.AgentSession(repo_root=tmp_path, journal_path=tmp_path / "j2.jsonl",
                                       transcript_path=tmp_path / "t2.jsonl", model="claude-haiku-5-5", max_turns=5)
    await plain.run("go")
    assert seen["partial"] is False
    assert "activity" not in (tmp_path / "j2.jsonl").read_text(encoding="utf-8")
