"""Staging a job's composition workspace for the kit.

Structure only: the entry file, an empty scene registry, an edit skeleton and a copy of a
starter look. The look is the one piece of creative code that is copied - it is meant to be
restyled per video, and `kit check` refuses a final render while it is untouched.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from lib.videokit import KIT_DIR, REPO_ROOT

DEFAULT_LOOK = "pop"

INDEX_TS = """import {registerRoot} from 'remotion';
import {makeRoot} from '@kit/engine';
import {look} from './look';
import {scenes} from './scenes';

registerRoot(makeRoot({look, scenes}));
"""

SCENES_TS = """// Scenes written for this video. Put each in src/scenes/<Name>.tsx, add it here, and name it
// in the edit: {"scene": "MyScene", "props": {...}}. They may import from '@kit/engine'
// (useShot, useWordFrame, PushIn, fill, clamp01, ...), '@kit/scenes' and './look'.
import type {SceneRegistry} from '@kit/engine';

export const scenes: SceneRegistry = {};
"""

EDIT_SKELETON = {
    "canvas": {"size": "1080x1920", "fps": 30, "safe": "tiktok"},
    "voice": {"lines": []},
    "captions": {"zone": "lower"},
    "main": [],
    "overlays": [],
}


def looks() -> list[str]:
    root = KIT_DIR / "looks"
    return sorted(p.name for p in root.iterdir() if (p / "tokens.ts").is_file()) if root.is_dir() else []


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def look_is_untouched(look_dir: Path) -> str | None:
    """The starter this look is still identical to (every file), or None once it was edited."""
    for name in looks():
        starter = KIT_DIR / "looks" / name
        files = sorted(p.name for p in starter.iterdir() if p.is_file())
        if all((look_dir / f).is_file() and _digest(look_dir / f) == _digest(starter / f) for f in files):
            return name
    return None


def set_look(project_dir: Path, name: str, *, force: bool = False) -> Path:
    """Copy a starter look into the job. Refuses to overwrite a look that was already restyled."""
    if name not in looks():
        raise ValueError(f"no starter look named {name!r}; there are: {', '.join(looks())}")
    target = project_dir / "composition" / "src" / "look"
    if target.exists():
        if look_is_untouched(target) is None and not force:
            raise ValueError("the look in composition/src/look has been edited; pass --force to replace it")
        shutil.rmtree(target)
    shutil.copytree(KIT_DIR / "looks" / name, target)
    return target


def _tsconfig() -> dict:
    kit = KIT_DIR.as_posix()
    return {
        "compilerOptions": {
            "target": "ES2022", "lib": ["ES2022", "DOM", "DOM.Iterable"], "module": "ESNext", "moduleResolution": "bundler",
            "jsx": "react-jsx", "strict": True, "noEmit": True, "allowImportingTsExtensions": True,
            "esModuleInterop": True, "skipLibCheck": True, "baseUrl": ".", "paths": {"@kit/*": [f"{kit}/*"]},
        },
        "include": ["src", f"{kit}/engine/assets.d.ts"],
    }


def stage_workspace(project_dir: Path, *, look: str = DEFAULT_LOOK) -> list[str]:
    """Create whatever the kit workspace is missing; never touch what is already there.

    Returns the paths (relative to the project) that were created."""
    comp = project_dir / "composition"
    src = comp / "src"
    made: list[str] = []

    def note(path: Path) -> None:
        made.append(path.relative_to(project_dir).as_posix())

    (src / "scenes").mkdir(parents=True, exist_ok=True)
    modules = comp / "node_modules"
    source_modules = REPO_ROOT / "remotion-composer" / "node_modules"
    if not modules.exists() and not modules.is_symlink() and source_modules.exists():
        modules.symlink_to(source_modules)
        note(modules)
    for path, text in (
        # "type": "module" lets the kit command read the look's tokens.ts directly.
        (comp / "package.json", json.dumps({"name": "kit-job", "private": True, "type": "module"}, indent=1) + "\n"),
        (comp / "tsconfig.json", json.dumps(_tsconfig(), indent=1) + "\n"),
        (src / "index.ts", INDEX_TS),
        (src / "scenes.ts", SCENES_TS),
        (comp / "edit.json", json.dumps(EDIT_SKELETON, indent=1) + "\n"),
    ):
        if not path.exists():
            path.write_text(text, encoding="utf-8")
            note(path)
    if not (src / "look").exists():
        note(set_look(project_dir, look))
    return made
