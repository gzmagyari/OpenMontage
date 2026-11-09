"""`kit`: one command for a video job. Run it in the project directory.

  kit init [--look NAME]     stage composition/ (entry file, scene registry, edit skeleton, look)
  kit look NAME [--force]    copy another starter look into the job (before restyling it)
  kit voice [--model base]   after the lines are spoken: prepare audio, time the words, lay the spine
  kit check [--draft]        check the edit; --draft accepts files that are still generating
  kit sheet [--draft]        one sheet of the telling frames, with layout findings
  kit mix                    the soundtrack: voice on the spine, music under it, -14 LUFS
  kit render [--name NAME]   the final video into exports/, verified
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from lib.videokit import KIT_DIR

NODE_COMMANDS = {"check", "sheet", "render", "resolve"}


def _option(args: list[str], name: str, default: str | None = None) -> str | None:
    flag = f"--{name}"
    if flag in args:
        index = args.index(flag)
        if index + 1 < len(args):
            return args[index + 1]
    return default


def _node(project: Path, command: str, rest: list[str]) -> int:
    return subprocess.call(["node", str(KIT_DIR / "cli" / "kit.mjs"), command, "--project", str(project), *rest])


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"help", "-h", "--help"}:
        print((__doc__ or "").strip())
        return 0
    command, rest = args[0], args[1:]
    project = Path(_option(rest, "project", ".") or ".").resolve()
    if "--project" in rest:
        index = rest.index("--project")
        del rest[index:index + 2]

    if command in NODE_COMMANDS:
        return _node(project, command, rest)

    if command == "init":
        from lib.videokit.stage import DEFAULT_LOOK, stage_workspace
        made = stage_workspace(project, look=_option(rest, "look", DEFAULT_LOOK) or DEFAULT_LOOK)
        print("Staged: " + (", ".join(made) if made else "nothing new (the workspace was already there)"))
        print("Write the edit in composition/edit.json, restyle composition/src/look/tokens.ts, then `kit check --draft`.")
        return 0

    if command == "look":
        from lib.videokit.stage import looks, set_look
        names = [a for a in rest if not a.startswith("--")]
        if not names:
            print("Starter looks: " + ", ".join(looks()))
            return 0
        try:
            set_look(project, names[0], force="--force" in rest)
        except ValueError as exc:
            print(f"kit: {exc}", file=sys.stderr)
            return 2
        print(f"composition/src/look is now the {names[0]!r} starter. Restyle tokens.ts for this video.")
        return 0

    if command == "voice":
        from lib.videokit import voice
        try:
            made = voice.prepare(project, model_size=_option(rest, "model", "base") or "base")
        except voice.VoiceError as exc:
            print(f"kit: {exc}", file=sys.stderr)
            return 2
        print(voice.summary(made))
        return 0

    if command == "mix":
        from lib.videokit import mix
        # The video's length comes from the resolved timeline, so sound and picture end together.
        total = None
        if _node(project, "resolve", ["--draft"]) == 0:
            try:
                resolved = json.loads((project / "work" / "kit" / "resolved.json").read_text(encoding="utf-8"))
                total = resolved["durationInFrames"] / resolved["fps"]
            except (OSError, ValueError, KeyError):
                total = None
        try:
            made = mix.build(project, total_seconds=total)
        except mix.MixError as exc:
            print(f"kit: {exc}", file=sys.stderr)
            return 2
        length = f"{made['seconds']:.2f} s" if made["seconds"] else "the length of the voice"
        music = f" with {made['music']}" if made["music"] else ", no music"
        print(f"Mixed {made['voice_parts']} voice part(s){music} into {made['file']} ({length}). The edit uses it automatically.")
        return 0

    print(f"kit: unknown command {command!r}. Try `kit help`.", file=sys.stderr)
    return 2
