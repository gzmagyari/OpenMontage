#!/usr/bin/env python3
"""Put another build of the API in place of the running one, on the Docker host.

    python3 deploy.py --spec /opt/openmontage/deploy.json --image REPOSITORY@sha256:DIGEST

In order: takes a lock, reads the spec, puts right what an interrupted run left behind, takes the
build's security profile out of the image, and stops there when the running container already has
this build's contents and these settings. Otherwise it waits (bounded) for a moment with no job in
production, stops the container, keeps it aside, starts the new build and waits for /readyz. A
build that does not get ready is removed and the previous container is put back. In both cases the
images of this repository other than the new and the previous one are deleted afterwards.

docker/deploy/server-entry.sh is how a pipeline reaches it; docs/API.md ("Deploying from the
pipeline") has the whole picture.

Two rules for whoever edits this file:

* It runs under the host's own python3, which can be as old as 3.6. Standard library only, and
  nothing newer than 3.6 (no capture_output/text, no dataclasses, no walrus, no fromisoformat).
  The pipeline runs tests/deploy under 3.6 for that reason.
* Its output lands in a pipeline log that may be public. It prints its own plain status lines and
  nothing a container, Docker or a settings file said: no setting, no address, no path. What a
  failed start had to say is written to the release folder on the host instead.
"""
import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CONTAINER_PORT = 8080
PROFILE_IN_IMAGE = "/app/docker/seccomp-openmontage.json"
LABEL_FINGERPRINT = "org.openmontage.deploy.fingerprint"
LABEL_IMAGE = "org.openmontage.deploy.image"

EXIT_OK = 0
EXIT_FAILED = 1        # the build is not in place; what ran before runs again (or nothing did)
EXIT_DOWN = 2          # the build is not in place and the previous one did not come back
EXIT_REFUSED = 64      # the request or the spec is not acceptable; nothing was touched
EXIT_BUSY = 75         # another run holds the lock; nothing was touched

REPOSITORY = r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
ADDRESS = r"(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}"
SIZE = r"\d+[mMgG]"
RELEASE_NAME = r"\d{8}T\d{6}Z-[A-Za-z0-9_]{4,}"

# name: (kind, default). Kinds are checked in load_spec; a spec with any other key is refused.
FIELDS = {
    "repository": ("repository", None),      # the only image repository this host deploys
    "name": ("name", "openmontage-api"),     # the container's name
    "env_file": ("file", None),              # absolute path of the settings file given to the container
    "volume": ("volume", "openmontage-data"),  # a Docker volume, or an absolute folder, mounted at /data
    "publish": ("addresses", None),          # host addresses that reach the API, as "IP:PORT"
    "probe": ("address", None),              # where this script asks /readyz; default: the first of publish
    "cpuset": ("cpuset", None),              # optional --cpuset-cpus
    "memory": ("size", None),                # optional --memory
    "shm_size": ("size", "2g"),
    "stop_timeout": ("number", 40, 5, 600),      # how long a stopping container gets to park its jobs
    "ready_timeout": ("number", 180, 10, 1800),  # how long a started build gets to answer ready
    "stable_seconds": ("number", 5, 0, 120),     # it must still be ready this long after the first yes
    "quiet_wait": ("number", 600, 0, 7200),      # how long to wait for a moment with no job running
    "keep_releases": ("number", 10, 2, 100),     # how many release folders (records) to keep
}


class DeployError(Exception):
    """Something went wrong. The message is ours and is safe to print."""


class Refused(DeployError):
    """The request or the spec is not acceptable."""


def say(text):
    """One status line. A reader that has gone away (a dropped SSH connection) must not stop the run."""
    try:
        print(text, flush=True)
    except (OSError, ValueError):
        pass


def monotonic():
    return time.monotonic()


def sleep(seconds):
    time.sleep(seconds)


# ---- Docker -------------------------------------------------------------------------------------
class Answer(object):
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _run(argv, timeout):
    """Run one docker command. The tests put a stand-in here."""
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=timeout)
    return Answer(result.returncode, result.stdout, result.stderr)


def docker(*args, **options):
    """``docker <args>``. With check (the default) a refusal raises, naming the operation only:
    what Docker printed may repeat settings, so it never reaches our output."""
    check, timeout = options.get("check", True), options.get("timeout", 600)
    words = [str(arg) for arg in args]
    try:
        answer = _run(["docker"] + words, timeout)
    except subprocess.TimeoutExpired:
        if check:
            raise DeployError("Docker did not finish '%s' in time" % operation(words))
        return Answer(124)
    except OSError:
        raise DeployError("the docker command cannot be run on this host")
    if check and answer.returncode:
        raise DeployError("Docker refused '%s'" % operation(words))
    return answer


def operation(words):
    return " ".join(word for word in words[:2] if re.fullmatch(r"[a-z]+", word))


def container(name):
    answer = docker("inspect", "--type", "container", name, check=False)
    if answer.returncode:
        return None
    try:
        data = json.loads(answer.stdout)[0]
        state, config = data.get("State") or {}, data.get("Config") or {}
        return {"image": data["Image"], "running": bool(state.get("Running")),
                "restarts": int(data.get("RestartCount") or 0), "labels": config.get("Labels") or {}}
    except (ValueError, KeyError, IndexError, TypeError):
        raise DeployError("Docker's answer about the container cannot be read")


def image_facts(reference):
    """The image's id and what decides how a container made from it behaves (its layers and its
    run settings). Labels and dates are left out on purpose: two builds of the same files are the
    same build here."""
    answer = docker("image", "inspect", reference, check=False)
    if answer.returncode:
        return None
    try:
        data = json.loads(answer.stdout)[0]
        config = data.get("Config") or {}
        contents = {"layers": (data.get("RootFS") or {}).get("Layers") or []}
        for key in ("Env", "Cmd", "Entrypoint", "User", "WorkingDir", "Healthcheck", "Volumes", "ExposedPorts"):
            contents[key] = config.get(key)
        return {"id": data["Id"], "contents": contents}
    except (ValueError, KeyError, IndexError, TypeError):
        raise DeployError("Docker's answer about the image cannot be read")


# ---- The spec -----------------------------------------------------------------------------------
def load_spec(path):
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Refused("the spec cannot be read as JSON")
    if not isinstance(raw, dict):
        raise Refused("the spec is not a JSON object")
    unknown = sorted(key for key in raw if key not in FIELDS)
    if unknown:
        raise Refused("the spec has settings this version does not know: %s"
                      % ", ".join(key for key in unknown if re.fullmatch(r"[A-Za-z0-9_]{1,40}", str(key))))
    spec = {}
    for key, rule in FIELDS.items():
        value = raw.get(key, rule[1])
        if value is None:
            if key in ("repository", "env_file", "publish"):
                raise Refused("the spec lacks '%s'" % key)
            spec[key] = None
            continue
        if not valid(rule, value):
            raise Refused("the spec's '%s' is not acceptable" % key)
        spec[key] = value
    if not Path(spec["env_file"]).is_file():
        raise Refused("the settings file named by 'env_file' does not exist")
    if spec["probe"] is None:
        spec["probe"] = spec["publish"][0]
    spec["state_dir"] = path.resolve().parent
    return spec


def valid(rule, value):
    kind = rule[0]
    if kind == "number":
        return type(value) is int and rule[2] <= value <= rule[3]
    if kind == "addresses":
        return (isinstance(value, list) and 1 <= len(value) <= 4 and len(set(value)) == len(value)
                and all(valid(("address",), item) for item in value))
    if not isinstance(value, str):
        return False
    if kind == "repository":
        return re.fullmatch(REPOSITORY, value) is not None
    if kind == "name":
        return re.fullmatch(r"[a-z][a-z0-9-]{2,60}", value) is not None
    if kind == "file":
        return value.startswith("/") and re.fullmatch(r"[A-Za-z0-9_./-]{2,200}", value) is not None
    if kind == "volume":
        if value.startswith("/"):
            return re.fullmatch(r"[A-Za-z0-9_./-]{2,200}", value) is not None and ".." not in value
        return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", value) is not None
    if kind == "address":
        if re.fullmatch(ADDRESS, value) is None:
            return False
        host, port = value.rsplit(":", 1)
        return all(int(part) <= 255 for part in host.split(".")) and 1 <= int(port) <= 65535
    if kind == "cpuset":
        return re.fullmatch(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*", value) is not None
    if kind == "size":
        return re.fullmatch(SIZE, value) is not None
    return False


def check_image(spec, image):
    """The image must be a build of the spec's repository, named by digest or by tag."""
    pattern = re.escape(spec["repository"]) + r"(?:@sha256:[0-9a-f]{64}|:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})"
    if re.fullmatch(pattern, image or "") is None:
        raise Refused("the image is not a build of this host's repository")


# ---- What a container is started with -----------------------------------------------------------
def run_arguments(spec, image, profile, labels):
    args = ["run", "-d", "--name", spec["name"], "--init", "--restart", "unless-stopped",
            "--stop-timeout", str(spec["stop_timeout"]), "--shm-size", spec["shm_size"]]
    if spec["cpuset"]:
        args += ["--cpuset-cpus", spec["cpuset"]]
    if spec["memory"]:
        args += ["--memory", spec["memory"]]
    for address in spec["publish"]:
        args += ["-p", "%s:%d" % (address, CONTAINER_PORT)]
    # The three options a job's box needs (docker/Dockerfile explains them).
    args += ["--security-opt", "seccomp=%s" % profile,
             "--security-opt", "systempaths=unconfined",
             "--security-opt", "apparmor=unconfined",
             "--env-file", spec["env_file"],
             "-v", "%s:/data" % spec["volume"]]
    for key in sorted(labels):
        args += ["--label", "%s=%s" % (key, labels[key])]
    return args + [image]


def fingerprint(spec, contents, profile):
    """One value for "this build's contents, started this way, with these settings". The running
    container carries it as a label; a deploy that would arrive at the same value changes nothing."""
    digest = hashlib.sha256()
    shape = run_arguments(spec, "<image>", "<profile>", {})
    digest.update(json.dumps({"contents": contents, "run": shape}, sort_keys=True).encode("utf-8"))
    for path in (spec["env_file"], str(profile)):
        digest.update(b"\0")
        digest.update(Path(path).read_bytes())
    return digest.hexdigest()


def take_profile(image, release):
    """The security profile travels in the image, so it always matches the build."""
    target = release / "seccomp.json"
    carrier = docker("create", "--entrypoint", "/bin/true", image).stdout.strip()
    try:
        docker("cp", "%s:%s" % (carrier, PROFILE_IN_IMAGE), str(target))
    finally:
        docker("rm", "-v", carrier, check=False)
    try:
        json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DeployError("the build carries no usable security profile")
    return target


# ---- Asking the engine --------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def probe(address):
    """(status, body) of GET /readyz on a published address; (0, b"") when nothing answers."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        response = opener.open("http://%s/readyz" % address, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
        return 0, b""
    try:
        with response:
            return response.code, response.read(8192)
    except (OSError, http.client.HTTPException, AttributeError):
        return 0, b""


def ready(spec):
    """(is it ready, names of its failing checks, the raw answer)."""
    status, body = probe(spec["probe"])
    failing = []
    try:
        checks = json.loads(body.decode("utf-8", "replace")).get("checks") or {}
        failing = sorted(str(name) for name, passed in checks.items()
                         if not passed and re.fullmatch(r"[a-z0-9_]{1,40}", str(name)))
    except (ValueError, AttributeError):
        pass
    return status == 200, failing, body


def wait_ready(spec):
    """Wait until the named container answers ready and stays so. Returns (ready, why not, failing
    checks, raw answer); why not is "gone", "stopping" (it keeps stopping) or "time"."""
    deadline = monotonic() + spec["ready_timeout"]
    failing, body = [], b""
    while True:
        state = container(spec["name"])
        if state is None:
            return False, "gone", failing, body
        if state["restarts"] >= 3:
            return False, "stopping", failing, body
        good, failing, body = ready(spec)
        if good:
            sleep(spec["stable_seconds"])
            good, failing, body = ready(spec)
            if good:
                return True, None, [], body
        if monotonic() >= deadline:
            return False, "time", failing, body
        sleep(2)


def jobs_running(name):
    """How many jobs the running engine has in production; None when it does not say. Asked inside
    the container with the container's own token, so no credential passes through here."""
    command = ('curl -fsS -m 5 -H "Authorization: Bearer ${OPENMONTAGE_API_TOKEN:-}" '
               "http://127.0.0.1:%d/v1/stats" % CONTAINER_PORT)
    answer = docker("exec", name, "sh", "-c", command, check=False, timeout=30)
    if answer.returncode:
        return None
    try:
        return int(json.loads(answer.stdout)["running"])
    except (ValueError, KeyError, TypeError):
        return None


def wait_for_quiet(spec):
    """A stop parks a running job and the next start continues it, which costs that job time. So a
    moment with none running is preferred, for at most quiet_wait seconds."""
    limit = spec["quiet_wait"]
    started, told = monotonic(), False
    while limit > 0:
        running = jobs_running(spec["name"])
        if not running:
            return
        if monotonic() - started >= limit:
            say("still in production after %d min: going ahead, the engine continues it after the start"
                % max(1, limit // 60))
            return
        if not told:
            say("%d job(s) in production: waiting up to %d min for a quiet moment" % (running, max(1, limit // 60)))
            told = True
        sleep(10)


# ---- Housekeeping -------------------------------------------------------------------------------
def put_right(spec):
    """An earlier run that died half-way can leave the previous container set aside. Either the new
    one is fine (then the old one is a leftover) or the old one goes back."""
    name, aside = spec["name"], spec["name"] + "-previous"
    if container(aside) is None:
        return
    current = container(name)
    if current is not None and current["running"] and ready(spec)[0]:
        docker("rm", "-f", aside)
        say("removed a leftover of an earlier run")
        return
    if current is not None:
        docker("rm", "-f", name)
    docker("rename", aside, name)
    docker("start", name)
    say("an earlier run had stopped half-way: the previous build was started again")
    wait_ready(spec)


def remove_old_images(spec, keep):
    """Delete this repository's images other than ``keep`` (image ids). Never forced, so an image a
    container still uses stays; and never an image of another repository."""
    answer = docker("image", "ls", "--no-trunc", "--digests", "--format",
                    "{{.ID}}\t{{.Repository}}\t{{.Tag}}\t{{.Digest}}", spec["repository"], check=False)
    if answer.returncode:
        return 0
    removed = set()
    for line in answer.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or parts[1] != spec["repository"] or parts[0] in keep:
            continue
        identity, repository, tag, digest = parts
        references = []
        if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
            references.append("%s:%s" % (repository, tag))
        if re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            references.append("%s@%s" % (repository, digest))
        for reference in references:
            docker("rmi", reference, check=False)
        if references and image_facts(identity) is None:
            removed.add(identity)
    return len(removed)


def new_release(state_dir):
    releases = state_dir / "releases"
    releases.mkdir(mode=0o700, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for attempt in range(100):
        folder = releases / ("%s-%s%02d" % (stamp, hashlib.sha256(os.urandom(8)).hexdigest()[:6], attempt))
        try:
            folder.mkdir(mode=0o700)
            return folder
        except FileExistsError:
            continue
    raise DeployError("no release folder could be made")


def prune_releases(spec, current):
    releases = spec["state_dir"] / "releases"
    try:
        folders = sorted(item for item in releases.iterdir()
                         if item.is_dir() and re.fullmatch(RELEASE_NAME, item.name) and item != current)
    except OSError:
        return
    for folder in folders[:max(0, len(folders) - (spec["keep_releases"] - 1))]:
        shutil.rmtree(str(folder), ignore_errors=True)


def write_private(path, data):
    mode = "wb" if isinstance(data, bytes) else "w"
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, mode) as stream:
        stream.write(data)


def keep_evidence(spec, release, body):
    """What the build that did not get ready had to say, for whoever looks on the host."""
    try:
        logs = docker("logs", "--tail", "300", spec["name"], check=False, timeout=60)
        write_private(release / "failed.log", (logs.stdout or "") + (logs.stderr or ""))
        write_private(release / "readyz.json", body or b"")
    except (OSError, DeployError):
        pass


def take_lock(state_dir):
    """One run at a time on this host. None when another run holds the lock."""
    import fcntl  # POSIX only, like every Docker host this runs on

    handle = open(str(state_dir / "deploy.lock"), "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


# ---- The deploy ---------------------------------------------------------------------------------
def deploy(spec, image, release):
    name, aside = spec["name"], spec["name"] + "-previous"
    record = {"image": image, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "outcome": "started"}

    def finish(outcome, code, **more):
        record.update(more, outcome=outcome, finished=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        try:
            write_private(release / "record.json", json.dumps(record, sort_keys=True, indent=2))
        except OSError:
            pass
        prune_releases(spec, release)
        return code

    put_right(spec)
    facts = image_facts(image)
    if facts is None:
        say("pulling the build")
        docker("pull", "-q", image, timeout=3600)
        facts = image_facts(image)
        if facts is None:
            raise DeployError("the build is not on this host after the pull")
    profile = take_profile(image, release)
    mark = fingerprint(spec, facts["contents"], profile)
    record["fingerprint"] = mark

    current = container(name)
    if (current is not None and current["running"] and current["labels"].get(LABEL_FINGERPRINT) == mark
            and ready(spec)[0]):
        say("the running engine already has these contents and settings: left running")
        return finish("unchanged", EXIT_OK)

    was_running = bool(current and current["running"])
    previous_image = current["image"] if current else None
    labels = {LABEL_FINGERPRINT: mark, LABEL_IMAGE: image}
    stage, good, why, failing, body, started = "before", False, None, [], b"", monotonic()
    try:
        if was_running:
            wait_for_quiet(spec)
            say("stopping the running engine")
            docker("stop", "-t", spec["stop_timeout"], name, timeout=spec["stop_timeout"] + 120)
        if current is not None:
            docker("rename", name, aside)
        stage, started = "aside", monotonic()
        say("starting the new build")
        docker(*run_arguments(spec, image, profile, labels))
        stage = "started"
        good, why, failing, body = wait_ready(spec)
    except DeployError as error:
        record["error"] = str(error)

    if good:
        if current is not None:
            docker("rm", "-f", aside, check=False)
        say("ready after %d s" % round(monotonic() - started))
        removed = remove_old_images(spec, {facts["id"], previous_image})
        if removed:
            say("removed %d older build(s) of this engine" % removed)
        return finish("ready", EXIT_OK, seconds=round(monotonic() - started))

    # Not in place. Whatever ran before is put back exactly as it was.
    if stage == "before":
        say("the running engine could not be stopped and set aside: nothing was changed")
        if was_running:
            docker("start", name, check=False)
        return finish("not_started", EXIT_FAILED)
    if stage == "started":
        keep_evidence(spec, release, body)
    docker("rm", "-f", name, check=False)
    if stage != "started" or why == "gone":
        say("the new build could not be started")
    elif why == "stopping":
        say("the new build keeps stopping")
    elif failing:
        say("the new build did not get ready; failing checks: %s" % ", ".join(failing))
    else:
        say("the new build did not get ready within %d s" % spec["ready_timeout"])
    remove_old_images(spec, {facts["id"], previous_image})
    if current is None:
        say("no engine is running: there was none before this deploy")
        return finish("failed", EXIT_FAILED, failing=failing)
    try:
        docker("rename", aside, name)
        if not was_running:
            say("the previous container is back in place, stopped as it was")
            return finish("failed", EXIT_FAILED, failing=failing)
        docker("start", name)
        back = wait_ready(spec)[0]
    except DeployError as error:
        record["restore_error"] = str(error)
        back = False
    if back:
        say("the previous build is running again")
        return finish("restored", EXIT_FAILED, failing=failing)
    say("the previous build did not come back: no engine is ready, this needs a person")
    return finish("down", EXIT_DOWN, failing=failing)


def self_check():
    """A few seconds of proof that this file works on the python it is given (the pipeline runs it
    under the oldest one supported)."""
    import tempfile

    folder = Path(tempfile.mkdtemp())
    try:
        (folder / "engine.env").write_text("A=1\n", encoding="utf-8")
        (folder / "seccomp.json").write_text("{}", encoding="utf-8")
        spec_file = folder / "deploy.json"
        spec_file.write_text(json.dumps({
            "repository": "ghcr.io/example/openmontage-api", "env_file": (folder / "engine.env").as_posix(),
            "publish": ["127.0.0.1:8710"], "cpuset": "0-3", "memory": "8g"}), encoding="utf-8")
        if os.name == "nt":  # a drive letter is no absolute POSIX path; the hosts this serves have none
            say("self-check skipped the spec on this platform (python %d.%d)" % sys.version_info[:2])
            return EXIT_OK
        spec = load_spec(spec_file)
        check_image(spec, "ghcr.io/example/openmontage-api@sha256:" + "0" * 64)
        arguments = run_arguments(spec, "image", folder / "seccomp.json", {LABEL_IMAGE: "image"})
        assert arguments[0] == "run" and arguments[-1] == "image" and "--cpuset-cpus" in arguments
        assert len(fingerprint(spec, {"layers": []}, folder / "seccomp.json")) == 64
    finally:
        shutil.rmtree(str(folder), ignore_errors=True)
    say("self-check ok (python %d.%d)" % sys.version_info[:2])
    return EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser(description="Put another build of the API in place of the running one.")
    parser.add_argument("--spec", help="the host's deploy.json")
    parser.add_argument("--image", help="the build: REPOSITORY@sha256:DIGEST (or REPOSITORY:TAG)")
    parser.add_argument("--release", help="a folder for this run's record (default: a new one beside the spec)")
    parser.add_argument("--self-check", action="store_true", help="check this file on this python and stop")
    options = parser.parse_args(argv)
    if options.self_check:
        return self_check()
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)  # a dropped connection must not stop a swap half-way
    lock = None
    try:
        if not options.spec or not options.image:
            raise Refused("--spec and --image are both needed")
        spec = load_spec(options.spec)
        check_image(spec, options.image)
        lock = take_lock(spec["state_dir"])
        if lock is None:
            say("another deploy is running on this host: nothing was touched")
            return EXIT_BUSY
        release = Path(options.release) if options.release else new_release(spec["state_dir"])
        if not release.is_dir():
            raise Refused("the release folder does not exist")
        return deploy(spec, options.image, release)
    except Refused as error:
        say("refused: %s" % error)
        return EXIT_REFUSED
    except DeployError as error:
        say("failed: %s" % error)
        return EXIT_FAILED
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    sys.exit(main())
