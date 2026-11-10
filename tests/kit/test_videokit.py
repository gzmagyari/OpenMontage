"""The video kit: workspace staging, voice preparation, the mix, and the engine's own tests.

The render test builds a small real video end to end (init, voice, mix, check, sheet,
render) from synthetic media made by ffmpeg; it runs where the image's Chrome and Node
modules are available and is skipped elsewhere.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lib.videokit import KIT_DIR, REPO_ROOT, stage, voice

NODE = shutil.which("node")
FFMPEG = shutil.which("ffmpeg")
_MODULES = REPO_ROOT / "remotion-composer" / "node_modules"
# The renderer's own Linux binary and its Chrome: present in the image, absent in a checkout
# whose node_modules were installed on another platform.
CAN_RENDER = bool(NODE and FFMPEG and sys.platform.startswith("linux") and (_MODULES / ".remotion").exists()
                  and any((_MODULES / "@remotion").glob("compositor-linux-*")))


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True, timeout=120)


# ---------------------------------------------------------------- the engine's own tests

@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_the_engine_unit_tests_pass():
    done = subprocess.run([str(NODE), "--test", str(KIT_DIR / "test" / "engine.test.ts")], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]


# ---------------------------------------------------------------- voice: words and splits

def test_words_fold_the_same_way_the_engine_folds_them():
    assert voice.norm("Re-renders,") == "rerenders"
    assert voice.norm("AI.") == "ai"
    assert voice.norm("·") == ""


def test_punctuation_alone_rides_with_the_word_before_it():
    assert voice.script_tokens("1 click · about 1 min") == ["1", "click ·", "about", "1", "min"]


def test_the_script_supplies_the_words_and_the_transcriber_the_clock():
    tokens = voice.script_tokens("Be honest: it grows 4x bigger, really.")
    heard = [
        {"word": "be", "start": 0.0, "end": 0.2}, {"word": "honest", "start": 0.2, "end": 0.6},
        {"word": "it", "start": 0.8, "end": 0.9}, {"word": "grows", "start": 0.9, "end": 1.3},
        {"word": "four", "start": 1.3, "end": 1.6}, {"word": "times", "start": 1.6, "end": 1.9},
        {"word": "bigger", "start": 1.9, "end": 2.4},
    ]
    words, share = voice.conform(tokens, heard, total=3.0)
    assert [w["text"] for w in words] == tokens  # the script's spelling and punctuation survive
    by = {w["text"]: (w["start"], w["end"]) for w in words}
    assert by["honest:"] == (0.2, 0.6)
    assert by["4x"] == (1.3, 1.9)            # heard as "four times": one token over that stretch
    assert by["bigger,"] == (1.9, 2.4)
    assert by["really."] == (2.4, 3.0)       # never heard: it takes what is left
    assert 0.7 < share < 0.8
    assert all(a["start"] <= b["start"] for a, b in zip(words, words[1:]))


def test_scripts_without_spaces_are_cut_into_words_and_timed_by_character():
    tokens = voice.script_tokens("今日は良い天気です。")
    assert "".join(tokens) == "今日は良い天気です。"
    assert 2 <= len(tokens) <= 9 and tokens[-1].endswith("。")  # the full stop rides with the last word
    # The transcriber cuts the same sentence differently (here: in two pieces).
    heard = [{"word": "今日は良い", "start": 0.0, "end": 1.0}, {"word": "天気です", "start": 1.0, "end": 2.0}]
    words, share = voice.conform(tokens, heard, total=2.2)
    assert [w["text"] for w in words] == tokens and share == 1.0
    assert words[0]["start"] == 0.0 and words[-1]["end"] == 2.0
    assert all(a["start"] <= b["start"] and a["end"] <= b["end"] + 1e-9 for a, b in zip(words, words[1:]))


def test_a_long_line_is_cut_at_a_sentence_pause():
    words = [{"text": t, "start": s, "end": s + 0.4} for t, s in [
        ("Upload", 0.0), ("it.", 1.0), ("Then", 2.0), ("wait,", 6.0), ("bigger.", 8.6),
        ("Faces,", 9.4), ("fur.", 12.0), ("One", 13.0), ("click.", 16.0)]]
    assert voice.split_points(words, 14.0) == []
    cuts = voice.split_points(words, 16.8)
    assert cuts == [pytest.approx(9.2)]      # after "bigger.", the sentence end nearest the middle
    with pytest.raises(voice.VoiceError):
        voice.split_points([{"text": "one", "start": 0.0, "end": 30.0}], 30.0)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_voice_preparation_lays_the_spine_and_splits_long_lines(tmp_path: Path):
    project = tmp_path
    audio = project / "assets" / "audio"
    audio.mkdir(parents=True)
    # Line A: 2 s of tone. Line B: 17 s of tone bursts with a pause in the middle.
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=330:duration=2", str(audio / "A.wav"))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=8.5", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-f", "lavfi", "-i", "sine=frequency=550:duration=8.3", "-filter_complex",
            "[1:a]atrim=duration=0.2[gap];[0:a][gap][2:a]concat=n=3:v=0:a=1", str(audio / "B.wav"))
    (project / "composition").mkdir()
    (project / "composition" / "edit.json").write_text(json.dumps({
        "voice": {"lines": [{"id": "A", "take": True, "text": "Hello there."},
                            {"id": "B", "take": True, "text": "First sentence here. Second sentence here."}]},
        "main": [{"take": "A"}, {"take": "B"}],
    }), encoding="utf-8")

    def heard(path: Path):
        if path.stem == "A":
            return [{"word": "hello", "start": 0.1, "end": 0.8}, {"word": "there", "start": 0.9, "end": 1.8}]
        return [{"word": "first", "start": 0.2, "end": 2.0}, {"word": "sentence", "start": 2.1, "end": 5.0},
                {"word": "here", "start": 5.1, "end": 8.3}, {"word": "second", "start": 8.8, "end": 11.0},
                {"word": "sentence", "start": 11.1, "end": 14.0}, {"word": "here", "start": 14.1, "end": 16.6}]

    made = voice.prepare(project, transcribe=heard)
    parts = [p for line in made["lines"] for p in line["parts"]]
    assert [p["id"] for p in parts] == ["A", "B_1", "B_2"]
    assert [p["take"] for p in parts] == ["assets/video/take_A.mp4", "assets/video/take_B_1.mp4", "assets/video/take_B_2.mp4"]
    assert all(p["dur"] <= voice.TAKE_LIMIT_SECONDS for p in parts)
    # The spine: every part starts where the one before it ended.
    for before, after in zip(parts, parts[1:]):
        assert after["start"] == pytest.approx(before["start"] + before["dur"], abs=0.002)
    assert made["voice_end"] == pytest.approx(parts[-1]["start"] + parts[-1]["dur"], abs=0.002)
    # Words keep the script's spelling, sit inside their part, and the split fell between the sentences.
    assert [w["text"] for w in parts[1]["words"]] == ["First", "sentence", "here."]
    assert [w["text"] for w in parts[2]["words"]] == ["Second", "sentence", "here."]
    for part in parts:
        assert all(part["start"] - 0.001 <= w["start"] <= part["start"] + part["dur"] + 0.001 for w in part["words"])
        assert (project / part["audio"]).is_file()
    assert any("split" in note for note in made["notes"])
    # The prepared audio is the take tool's own trim of the recording.
    from tools.avatar.fal_avatar import FalAvatar
    reference = FalAvatar._trim_silence(audio / "A.wav", tmp_path / "ref.wav")
    assert voice.duration_of(project / parts[0]["audio"]) == pytest.approx(voice.duration_of(reference), abs=0.002)
    # A second run with nothing changed reuses the timings.
    again = voice.prepare(project, transcribe=lambda path: pytest.fail("the unchanged line was timed again"))
    assert [(p["id"], p["start"], p["dur"]) for l in again["lines"] for p in l["parts"]] == [(p["id"], p["start"], p["dur"]) for p in parts]
    assert again["lines"][1]["parts"][1]["words"] == made["lines"][1]["parts"][1]["words"]


def test_a_missing_recording_says_where_to_put_it(tmp_path: Path):
    (tmp_path / "composition").mkdir()
    (tmp_path / "composition" / "edit.json").write_text(json.dumps({"voice": {"lines": [{"id": "A", "text": "Hi."}]}, "main": []}), encoding="utf-8")
    with pytest.raises(voice.VoiceError, match="assets/audio/A.mp3"):
        voice.prepare(tmp_path, transcribe=lambda path: [])


# ---------------------------------------------------------------- staging

def test_staging_creates_the_workspace_once_and_never_overwrites(tmp_path: Path):
    made = stage.stage_workspace(tmp_path, look="pop")
    comp = tmp_path / "composition"
    for rel in ("package.json", "tsconfig.json", "edit.json", "src/index.ts", "src/scenes.ts", "src/look/tokens.ts", "src/look/look.tsx"):
        assert (comp / rel).is_file(), rel
    assert "composition/edit.json" in made and "composition/src/look" in made
    assert "@kit/engine" in (comp / "src" / "index.ts").read_text(encoding="utf-8")
    assert json.loads((comp / "tsconfig.json").read_text(encoding="utf-8"))["compilerOptions"]["paths"]["@kit/*"] == [f"{KIT_DIR.as_posix()}/*"]
    (comp / "edit.json").write_text('{"main": [{"color": "#000", "for": 1}]}', encoding="utf-8")
    assert stage.stage_workspace(tmp_path) == []
    assert "color" in (comp / "edit.json").read_text(encoding="utf-8")


def test_a_restyled_look_is_not_replaced_without_force(tmp_path: Path):
    stage.stage_workspace(tmp_path, look="pop")
    look_dir = tmp_path / "composition" / "src" / "look"
    assert stage.look_is_untouched(look_dir) == "pop"
    stage.set_look(tmp_path, "editorial")  # still untouched: switching is free
    assert stage.look_is_untouched(look_dir) == "editorial"
    tokens = look_dir / "tokens.ts"
    tokens.write_text(tokens.read_text(encoding="utf-8").replace("#FFC23D", "#3DDCFF"), encoding="utf-8")
    assert stage.look_is_untouched(look_dir) is None
    with pytest.raises(ValueError, match="has been edited"):
        stage.set_look(tmp_path, "pop")
    stage.set_look(tmp_path, "pop", force=True)
    assert stage.look_is_untouched(look_dir) == "pop"
    with pytest.raises(ValueError, match="no starter look"):
        stage.set_look(tmp_path, "nope", force=True)


def test_the_ready_made_scenes_are_listed_where_the_checker_reads_them():
    import re
    registry = re.search(r"builtinScenes: SceneRegistry = \{([^}]*)\}", (KIT_DIR / "scenes" / "index.ts").read_text(encoding="utf-8"))
    listed = re.search(r"BUILTIN_SCENES = \[([^\]]*)\]", (KIT_DIR / "engine" / "types.ts").read_text(encoding="utf-8"))
    assert registry and listed
    assert sorted(n.strip() for n in registry.group(1).split(",")) == sorted(n.strip(" '") for n in listed.group(1).split(","))


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_a_video_without_a_voice_gets_its_music_cut_and_faded(tmp_path: Path):
    from lib.videokit import mix
    (tmp_path / "composition").mkdir()
    (tmp_path / "assets").mkdir()
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=220:duration=12", str(tmp_path / "assets" / "bed.wav"))
    (tmp_path / "composition" / "edit.json").write_text(json.dumps({"music": {"file": "assets/bed.wav"}, "main": [{"color": "#000", "for": 5}]}), encoding="utf-8")
    made = mix.build(tmp_path, total_seconds=5.0)
    assert made["voice_parts"] == 0 and made["music"] == "assets/bed.wav"
    assert voice.duration_of(tmp_path / "assets/audio/mix.wav") == pytest.approx(5.0, abs=0.05)
    (tmp_path / "composition" / "edit.json").write_text(json.dumps({"main": [{"color": "#000", "for": 5}]}), encoding="utf-8")
    with pytest.raises(mix.MixError, match="nothing to mix"):
        mix.build(tmp_path, total_seconds=5.0)


def test_every_font_the_kit_ships_is_a_declared_dependency():
    import re
    source = (KIT_DIR / "engine" / "fonts.ts").read_text(encoding="utf-8")
    package = json.loads((REPO_ROOT / "remotion-composer" / "package.json").read_text(encoding="utf-8"))
    imported = re.findall(r"from '(@fontsource(?:-variable)?/[^/']+)/(files/[^']+\.woff2)'", source)
    assert len(imported) >= 8
    for name, file in imported:
        version = package["dependencies"].get(name)
        assert version and version[0].isdigit(), f"{name} must be pinned to an exact version"
        installed = _MODULES / name
        if installed.is_dir():  # where the Node modules are installed, the files must really be there
            assert (installed / file).is_file(), f"{name}/{file}"


def test_every_starter_look_is_complete():
    assert {"pop", "editorial"} <= set(stage.looks())
    for name in stage.looks():
        folder = KIT_DIR / "looks" / name
        tokens = (folder / "tokens.ts").read_text(encoding="utf-8")
        assert "export const tokens" in tokens and "export const meta" in tokens
        # tokens.ts is read by the kit command without a bundler: it may not import anything.
        assert "import " not in tokens, name
        assert "export const look" in (folder / "look.tsx").read_text(encoding="utf-8")


# ---------------------------------------------------------------- end to end

def _kit(project: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "lib.videokit", *args, "--project", str(project)],
                          cwd=REPO_ROOT, capture_output=True, text=True, timeout=600)


@pytest.mark.skipif(not CAN_RENDER, reason="needs the image's Chrome and Node modules (Linux)")
def test_a_small_video_from_init_to_export(tmp_path: Path):
    project = tmp_path
    assert _kit(project, "init", "--look", "pop").returncode == 0
    (project / "assets" / "audio").mkdir(parents=True)
    (project / "assets" / "video").mkdir(parents=True)
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=330:duration=2.5", str(project / "assets/audio/A.wav"))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=220:duration=8", str(project / "assets/audio/music.wav"))
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=540x960:rate=30:duration=4", "-pix_fmt", "yuv420p", str(project / "assets/video/clip.mp4"))
    edit = {
        "canvas": {"size": "540x960", "fps": 30, "safe": "tiktok"},
        "voice": {"lines": [{"id": "A", "text": "A tone stands in for the voice."}]},
        "music": {"file": "assets/audio/music.wav"},
        "main": [{"video": "assets/video/clip.mp4", "until": {"word": "stands"}},
                 {"color": "#203040", "until": {"voice": "end"}, "enter": "slide-up"},
                 {"video": "assets/video/clip.mp4", "start": 1, "for": 1}],
        "overlays": [{"tag": "hello", "from": 0.3, "for": 1.2}],
    }
    (project / "composition" / "edit.json").write_text(json.dumps(edit), encoding="utf-8")

    untouched = _kit(project, "check", "--draft")
    assert "look-untouched" in untouched.stdout
    tokens = project / "composition" / "src" / "look" / "tokens.ts"
    tokens.write_text(tokens.read_text(encoding="utf-8").replace("#FFD400", "#35E0A1"), encoding="utf-8")
    # Restyle the type too, with a font the kit ships: a font that fails to load fails the render.
    look = project / "composition" / "src" / "look" / "look.tsx"
    styled = look.read_text(encoding="utf-8").replace(
        "const FONT = tokens.font.display;", "import {fonts as shipped} from '@kit/engine'; const FONT = shipped.anton();")
    assert "shipped.anton()" in styled
    look.write_text(styled, encoding="utf-8")

    spoken = _kit(project, "voice")
    assert spoken.returncode == 0, spoken.stdout + spoken.stderr
    mixed = _kit(project, "mix")
    assert mixed.returncode == 0, mixed.stdout + mixed.stderr
    assert (project / "assets/audio/mix.wav").is_file()

    checked = _kit(project, "check")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    sheet = _kit(project, "sheet")
    assert sheet.returncode == 0, sheet.stdout + sheet.stderr
    assert (project / "work/kit/sheet.jpg").is_file()
    # The layout probe talks to the command, not to whoever reads its output.
    assert "KITDIAG" not in sheet.stdout + sheet.stderr

    rendered = _kit(project, "render", "--name", "smoke")
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    out = project / "exports" / "smoke.mp4"
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,color_space:format=duration",
                                       "-of", "json", str(out)], capture_output=True, text=True, check=True).stdout)
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert (video["codec_name"], video["width"], video["height"], video["color_space"]) == ("h264", 540, 960, "bt709")
    assert any(s["codec_type"] == "audio" for s in probe["streams"])
    assert float(probe["format"]["duration"]) == pytest.approx(voice.duration_of(project / "assets/audio/voice_A.wav") + 1.0, abs=0.2)


@pytest.mark.skipif(not CAN_RENDER, reason="needs the image's Chrome and Node modules (Linux)")
def test_a_draft_holds_up_while_files_and_scenes_are_still_being_made(tmp_path: Path):
    """A real run sheets its layout while the paid batch is still generating: pictures and clips
    that scenes take in their props do not exist yet, and neither does a scene someone is still
    writing. The draft must show placeholders for all of them and put nothing at a real path."""
    project = tmp_path
    assert _kit(project, "init", "--look", "pop").returncode == 0
    edit = {
        "canvas": {"size": "540x960", "fps": 30, "safe": "tiktok"},
        "music": {"file": "assets/audio/music.mp3"},
        "captions": {"enabled": False},
        "main": [
            {"scene": "KineticText", "props": {"text": "Closed\nSaturday", "backdrop": {"video": "assets/video/shop.mp4", "dim": 0.4}}, "for": 2},
            {"scene": "BeforeAfter", "props": {"pair": "refs/room"}, "for": 2, "enter": "punch"},
            {"scene": "Stamp", "props": {"word": "10 AM", "backdrop": "assets/images/door.png"}, "for": 2},
        ],
        "overlays": [{"tag": "soon", "from": 0.4, "for": 1.2}],
    }
    edit_file = project / "composition" / "edit.json"
    edit_file.write_text(json.dumps(edit), encoding="utf-8")
    missing = ["assets/audio/music.mp3", "assets/images/door.png", "assets/video/shop.mp4", "refs/room_after.jpg", "refs/room_before.jpg"]

    draft = _kit(project, "check", "--draft")
    assert draft.returncode == 0, draft.stdout + draft.stderr
    assert f"Still generating (5): {', '.join(missing)}" in draft.stdout
    assert "Still to write (1): scene Stamp" in draft.stdout and "scene-unwritten" in draft.stdout
    assert "no voice" in draft.stdout

    sheet = _kit(project, "sheet", "--draft")
    assert sheet.returncode == 0, sheet.stdout + sheet.stderr
    assert (project / "work/kit/sheet.jpg").is_file()
    assert "KITDIAG" not in sheet.stdout + sheet.stderr
    # The stand-ins live in the bundle only: nothing was put where the real files belong.
    assert not any((project / rel).exists() for rel in missing)
    assert (project / "work/kit/public/assets/video/shop.mp4").is_file()

    final = _kit(project, "check", "--allow-starter-look")
    assert final.returncode == 1
    assert "MISSING (5)" in final.stdout and "scene-unknown" in final.stdout and "asset-missing" in final.stdout
    refused = _kit(project, "render", "--allow-starter-look")
    assert refused.returncode == 1 and "Nothing was rendered" in refused.stdout
    assert not list((project / "exports").glob("*.mp4"))

    # A misspelt ready-made scene is not "still to write": it is an error straight away.
    edit["main"][0]["scene"] = "KineticTxt"
    edit_file.write_text(json.dumps(edit), encoding="utf-8")
    typo = _kit(project, "check", "--draft")
    assert typo.returncode == 1 and 'Did you mean "KineticText"?' in typo.stdout


@pytest.mark.skipif(not CAN_RENDER, reason="needs the image's Chrome and Node modules (Linux)")
def test_the_final_check_wants_a_soundtrack_that_fits_the_video(tmp_path: Path):
    project = tmp_path
    assert _kit(project, "init", "--look", "pop").returncode == 0
    (project / "assets" / "audio").mkdir(parents=True)
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=220:duration=8", str(project / "assets/audio/music.wav"))
    edit = {"canvas": {"size": "540x960", "fps": 30, "safe": "tiktok"}, "music": {"file": "assets/audio/music.wav"},
            "captions": {"enabled": False}, "main": [{"color": "#203040", "for": 3}]}
    edit_file = project / "composition" / "edit.json"
    edit_file.write_text(json.dumps(edit), encoding="utf-8")

    def final() -> subprocess.CompletedProcess:
        return _kit(project, "check", "--allow-starter-look")

    # Music is named but `kit mix` never ran: the export would be silent.
    silent = final()
    assert silent.returncode == 1 and "mix-missing" in silent.stdout
    assert _kit(project, "check", "--draft").returncode == 0  # a draft does not ask for it yet
    assert _kit(project, "mix").returncode == 0
    assert final().returncode == 0, final().stdout

    # The video gets longer after the mix: the sound would stop two seconds early.
    edit["main"][0]["for"] = 5
    edit_file.write_text(json.dumps(edit), encoding="utf-8")
    stale = final()
    assert stale.returncode == 1 and "mix-stale" in stale.stdout and "kit mix" in stale.stdout
    drafted = _kit(project, "check", "--draft")
    assert drafted.returncode == 0 and "mix-stale" in drafted.stdout  # a warning there
    assert _kit(project, "mix").returncode == 0
    assert final().returncode == 0, final().stdout
