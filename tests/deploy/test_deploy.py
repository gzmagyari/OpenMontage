"""docker/deploy/deploy.py against a pretend Docker host.

The helper runs under the host's own python3, which can be as old as 3.6, so this file keeps to
unittest and to 3.6 too: the pipeline runs it under 3.6 as well as in the normal suite.

    python -m unittest discover -s tests/deploy
"""
import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("openmontage_deploy", str(ROOT / "docker" / "deploy" / "deploy.py"))
deploy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deploy)

REPOSITORY = "ghcr.io/example/openmontage-api"
OTHER = "ghcr.io/example/another-product"
SECRET = "sk-settings-value-that-must-never-be-printed"


def digest_of(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class PretendDocker(object):
    """Enough of the docker command line for the helper: images, containers, and every call made."""

    def __init__(self):
        self.images = {}       # id -> {"layers", "references": set, "ready": bool, "checks": dict}
        self.remote = {}       # reference -> image, for builds a pull would bring
        self.containers = {}   # name -> {"image", "running", "labels", "arguments", "restarts"}
        self.calls = []
        self.refuse = set()    # first words of commands that fail
        self.jobs = []         # answers to "how many jobs are running", in order; the last one repeats
        self.profile = '{"defaultAction": "SCMP_ACT_ERRNO"}'
        self.carriers = {}

    # -- setting the scene --
    def build(self, name, layers=None, ready=True, checks=None, repository=REPOSITORY, pulled=True):
        """A build named by digest. Returns its reference."""
        reference = "%s@%s" % (repository, digest_of("manifest " + name))
        image = {"id": digest_of("image " + name), "layers": layers or [digest_of("layer " + name)],
                 "references": {reference}, "ready": ready, "checks": checks or {}}
        if pulled:
            self.images[image["id"]] = image
        else:
            self.remote[reference] = image
        return reference

    def image(self, reference):
        for image in self.images.values():
            if reference == image["id"] or reference in image["references"]:
                return image
        return None

    def ran(self, *words):
        """The calls that began with these words."""
        return [call for call in self.calls if tuple(call[:len(words)]) == words]

    # -- the command line --
    def __call__(self, argv, timeout):
        assert argv[0] == "docker"
        words = list(argv[1:])
        self.calls.append(words)
        if words[0] in self.refuse:
            return deploy.Answer(1, "", "refused, and here is a setting: " + SECRET)
        handler = getattr(self, "do_" + words[0])
        return handler(words[1:])

    def ok(self, text=""):
        return deploy.Answer(0, text, "")

    def no(self):
        return deploy.Answer(1, "", "Error: no such object, by the way " + SECRET)

    def do_inspect(self, words):
        box = self.containers.get(words[-1])
        if box is None:
            return self.no()
        return self.ok(json.dumps([{"Image": box["image"], "RestartCount": box["restarts"],
                                    "State": {"Running": box["running"]}, "Config": {"Labels": box["labels"]}}]))

    def do_image(self, words):
        if words[0] == "inspect":
            image = self.image(words[1])
            if image is None:
                return self.no()
            return self.ok(json.dumps([{"Id": image["id"], "RootFS": {"Layers": image["layers"]},
                                        "Config": {"Env": ["PATH=/usr/bin"], "Cmd": ["uvicorn"], "Labels": {"build": image["id"]}}}]))
        assert words[0] == "ls"
        rows = []
        for image in self.images.values():
            for reference in sorted(image["references"]):
                repository, _, digest = reference.partition("@")
                if repository == words[-1]:
                    rows.append("%s\t%s\t<none>\t%s" % (image["id"], repository, digest))
        return self.ok("\n".join(rows) + "\n")

    def do_pull(self, words):
        image = self.remote.pop(words[-1], None)
        if image is None:
            return self.no()
        self.images[image["id"]] = image
        return self.ok()

    def do_create(self, words):
        if self.image(words[-1]) is None:
            return self.no()
        carrier = "carrier%d" % (len(self.carriers) + 1)
        self.carriers[carrier] = words[-1]
        return self.ok(carrier + "\n")

    def do_cp(self, words):
        source, target = words
        assert source.split(":", 1)[0] in self.carriers and source.endswith(deploy.PROFILE_IN_IMAGE)
        Path(target).write_text(self.profile, encoding="utf-8")
        return self.ok()

    def do_rm(self, words):
        name = words[-1]
        if name in self.carriers:
            del self.carriers[name]
            return self.ok()
        return self.ok() if self.containers.pop(name, None) is not None else self.no()

    def do_exec(self, words):
        box = self.containers.get(words[0])
        if box is None or not box["running"] or not self.jobs:
            return self.no()
        running = self.jobs.pop(0) if len(self.jobs) > 1 else self.jobs[0]
        return self.ok(json.dumps({"running": running, "queued": 0}))

    def do_stop(self, words):
        box = self.containers.get(words[-1])
        if box is None:
            return self.no()
        box["running"] = False
        return self.ok()

    def do_rename(self, words):
        old, new = words
        if old not in self.containers or new in self.containers:
            return self.no()
        self.containers[new] = self.containers.pop(old)
        return self.ok()

    def do_start(self, words):
        box = self.containers.get(words[-1])
        if box is None:
            return self.no()
        box["running"] = True
        return self.ok()

    def do_run(self, words):
        name = words[words.index("--name") + 1]
        image = self.image(words[-1])
        if image is None or name in self.containers:
            return self.no()
        labels = dict(words[index + 1].split("=", 1) for index, word in enumerate(words) if word == "--label")
        self.containers[name] = {"image": image["id"], "running": True, "labels": labels,
                                 "arguments": words, "restarts": 0}
        return self.ok("containerid\n")

    def do_logs(self, words):
        return self.ok("the engine's own words, with " + SECRET + "\n")

    def do_rmi(self, words):
        image = self.image(words[-1])
        if image is None or any(box["image"] == image["id"] for box in self.containers.values()):
            return self.no()
        image["references"].discard(words[-1])
        if not image["references"]:
            del self.images[image["id"]]
        return self.ok()

    # -- the engine's /readyz, as the helper's probe sees it --
    def probe(self, name):
        box = self.containers.get(name)
        if box is None or not box["running"]:
            return 0, b""
        image = self.images[box["image"]]
        if image["ready"]:
            return 200, json.dumps({"ready": True, "checks": {"ffmpeg": True}}).encode("utf-8")
        checks = dict({"ffmpeg": True}, **image["checks"])
        body = {"ready": False, "checks": checks, "problems": {"agent_box": "kernel detail " + SECRET}}
        return 503, json.dumps(body).encode("utf-8")


@unittest.skipIf(os.name == "nt", "the helper serves POSIX Docker hosts")
class DeployCase(unittest.TestCase):
    NAME = "openmontage-api"

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.folder), True)
        self.env_file = self.folder / "engine.env"
        self.env_file.write_text("ANTHROPIC_API_KEY=%s\n" % SECRET, encoding="utf-8")
        self.spec_file = self.folder / "deploy.json"
        self.settings = {"repository": REPOSITORY, "env_file": str(self.env_file),
                         "publish": ["172.17.0.1:8710", "127.0.0.1:8710"], "probe": "127.0.0.1:8710",
                         "cpuset": "14-19", "memory": "8g"}
        self.write_spec()
        self.docker = PretendDocker()
        self.said = []
        self.clock = 0.0
        originals = {name: getattr(deploy, name) for name in ("_run", "probe", "say", "monotonic", "sleep")}
        self.addCleanup(lambda: [setattr(deploy, name, value) for name, value in originals.items()])
        deploy._run = self.docker
        deploy.probe = lambda address: self.docker.probe(self.NAME)
        deploy.say = self.said.append
        deploy.monotonic = lambda: self.clock
        deploy.sleep = self.pass_time

    def pass_time(self, seconds):
        self.clock += seconds

    def write_spec(self, **changes):
        self.settings.update(changes)
        self.spec_file.write_text(json.dumps(self.settings), encoding="utf-8")

    def run_deploy(self, image, release=None):
        """One run of the helper. Remembers the release folder the run made, for record()."""
        before = set(self.releases())
        arguments = ["--spec", str(self.spec_file), "--image", image]
        code = deploy.main(arguments + (["--release", str(release)] if release else []))
        made = set(self.releases()) - before
        self.last_release = release or (made.pop() if len(made) == 1 else None)
        return code

    def box(self, name=None):
        return self.docker.containers.get(name or self.NAME)

    def releases(self):
        folder = self.folder / "releases"
        return sorted(folder.iterdir()) if folder.is_dir() else []

    def record(self):
        return json.loads((self.last_release / "record.json").read_text(encoding="utf-8"))

    def assert_nothing_private_was_said(self):
        text = "\n".join(self.said)
        for private in (SECRET, "172.17.0.1", "127.0.0.1", str(self.folder), "kernel detail"):
            self.assertNotIn(private, text)


class FirstStart(DeployCase):
    def test_a_host_with_no_engine_gets_one(self):
        build = self.docker.build("one")
        self.assertEqual(self.run_deploy(build), deploy.EXIT_OK)
        box = self.box()
        self.assertTrue(box["running"])
        arguments = box["arguments"]
        for expected in ("--init", "unless-stopped", "systempaths=unconfined", "apparmor=unconfined",
                         "172.17.0.1:8710:8080", "127.0.0.1:8710:8080", "14-19", "8g",
                         str(self.env_file), "openmontage-data:/data"):
            self.assertIn(expected, arguments)
        self.assertEqual(arguments[-1], build)
        profile = [word for word in arguments if word.startswith("seccomp=")][0].split("=", 1)[1]
        self.assertEqual(Path(profile).read_text(encoding="utf-8"), self.docker.profile)
        self.assertEqual(box["labels"][deploy.LABEL_IMAGE], build)
        self.assertEqual(self.record()["outcome"], "ready")
        self.assertEqual(self.docker.ran("stop"), [])
        self.assert_nothing_private_was_said()

    def test_a_build_that_is_not_on_the_host_is_pulled(self):
        build = self.docker.build("one", pulled=False)
        self.assertEqual(self.run_deploy(build), deploy.EXIT_OK)
        self.assertEqual(len(self.docker.ran("pull")), 1)
        self.assertTrue(self.box()["running"])

    def test_a_first_build_that_does_not_get_ready_leaves_nothing_running(self):
        build = self.docker.build("one", ready=False, checks={"agent_box": False, "fal_key": False})
        self.assertEqual(self.run_deploy(build), deploy.EXIT_FAILED)
        self.assertEqual(self.docker.containers, {})
        self.assertTrue(any("agent_box, fal_key" in line for line in self.said))
        self.assertTrue(any("no engine is running" in line for line in self.said))
        log = (self.last_release / "failed.log").read_text(encoding="utf-8")
        self.assertIn(SECRET, log)  # what the container said stays on the host
        self.assertEqual(self.record()["outcome"], "failed")
        self.assert_nothing_private_was_said()


class Swap(DeployCase):
    def setUp(self):
        super(Swap, self).setUp()
        self.old = self.docker.build("old")
        self.assertEqual(self.run_deploy(self.old), deploy.EXIT_OK)
        self.docker.calls[:] = []
        self.said[:] = []

    def test_a_new_build_replaces_the_running_one(self):
        new = self.docker.build("new")
        self.assertEqual(self.run_deploy(new), deploy.EXIT_OK)
        self.assertEqual(self.box()["labels"][deploy.LABEL_IMAGE], new)
        self.assertIsNone(self.box(self.NAME + "-previous"))
        order = [call[0] for call in self.docker.calls if call[0] in ("stop", "rename", "run")]
        self.assertEqual(order, ["stop", "rename", "run"])
        self.assertEqual(self.docker.ran("stop")[0][:2], ["stop", "-t"])
        self.assert_nothing_private_was_said()

    def test_the_same_build_again_changes_nothing(self):
        self.assertEqual(self.run_deploy(self.old), deploy.EXIT_OK)
        for word in ("stop", "rename", "run", "rmi"):
            self.assertEqual(self.docker.ran(word), [])
        self.assertTrue(any("left running" in line for line in self.said))
        self.assertEqual(self.record()["outcome"], "unchanged")

    def test_the_same_files_under_another_digest_change_nothing(self):
        same = self.docker.build("rebuilt", layers=[digest_of("layer old")])
        self.assertEqual(self.run_deploy(same), deploy.EXIT_OK)
        self.assertEqual(self.docker.ran("run"), [])
        self.assertEqual(self.box()["labels"][deploy.LABEL_IMAGE], self.old)

    def test_a_changed_settings_file_starts_the_same_build_anew(self):
        self.env_file.write_text("ANTHROPIC_API_KEY=%s\nAGENT_CONCURRENCY=2\n" % SECRET, encoding="utf-8")
        self.assertEqual(self.run_deploy(self.old), deploy.EXIT_OK)
        self.assertEqual(len(self.docker.ran("run")), 1)

    def test_a_changed_run_value_starts_the_same_build_anew(self):
        self.write_spec(memory="6g")
        self.assertEqual(self.run_deploy(self.old), deploy.EXIT_OK)
        self.assertIn("6g", self.box()["arguments"])

    def test_a_build_that_does_not_get_ready_gives_way_to_the_previous_one(self):
        bad = self.docker.build("bad", ready=False, checks={"anthropic_key": False})
        self.assertEqual(self.run_deploy(bad), deploy.EXIT_FAILED)
        box = self.box()
        self.assertTrue(box["running"])
        self.assertEqual(box["labels"][deploy.LABEL_IMAGE], self.old)
        self.assertIsNone(self.box(self.NAME + "-previous"))
        self.assertTrue(any("failing checks: anthropic_key" in line for line in self.said))
        self.assertTrue(any("previous build is running again" in line for line in self.said))
        self.assertEqual(self.record()["outcome"], "restored")
        self.assertGreaterEqual(self.clock, 180)  # it was given its whole time to get ready
        self.assert_nothing_private_was_said()

    def test_a_build_docker_will_not_start_gives_way_to_the_previous_one(self):
        new = self.docker.build("new")
        self.docker.refuse.add("run")
        self.assertEqual(self.run_deploy(new), deploy.EXIT_FAILED)
        self.assertTrue(any("could not be started" in line for line in self.said))
        self.assertEqual(self.box()["labels"][deploy.LABEL_IMAGE], self.old)
        self.assertTrue(self.box()["running"])
        self.assert_nothing_private_was_said()

    def test_an_engine_that_cannot_be_stopped_is_left_alone(self):
        new = self.docker.build("new")
        self.docker.refuse.add("stop")
        self.assertEqual(self.run_deploy(new), deploy.EXIT_FAILED)
        self.assertEqual(self.docker.ran("rename"), [])
        self.assertEqual(self.docker.ran("run"), [])
        self.assertEqual(self.box()["labels"][deploy.LABEL_IMAGE], self.old)

    def test_when_the_previous_build_does_not_come_back_it_says_so(self):
        bad = self.docker.build("bad", ready=False)
        self.docker.image(self.old)["ready"] = False
        self.assertEqual(self.run_deploy(bad), deploy.EXIT_DOWN)
        self.assertTrue(any("needs a person" in line for line in self.said))
        self.assertEqual(self.record()["outcome"], "down")

    def test_a_build_that_keeps_stopping_is_given_up_early(self):
        bad = self.docker.build("bad", ready=False)
        real_run = self.docker.do_run

        def run_then_loop(words):
            answer = real_run(words)
            self.box()["restarts"] = 5
            return answer

        self.docker.do_run = run_then_loop
        self.assertEqual(self.run_deploy(bad), deploy.EXIT_FAILED)
        self.assertLess(self.clock, 60)
        self.assertTrue(any("keeps stopping" in line for line in self.said))
        self.assertTrue(any("previous build is running again" in line for line in self.said))

    def test_a_stopped_engine_is_replaced_and_a_failure_leaves_it_stopped(self):
        self.docker.containers[self.NAME]["running"] = False
        bad = self.docker.build("bad", ready=False)
        self.assertEqual(self.run_deploy(bad), deploy.EXIT_FAILED)
        self.assertFalse(self.box()["running"])
        self.assertEqual(self.box()["labels"][deploy.LABEL_IMAGE], self.old)
        self.assertEqual(self.docker.ran("stop"), [])


class QuietMoment(DeployCase):
    def setUp(self):
        super(QuietMoment, self).setUp()
        self.old = self.docker.build("old")
        self.run_deploy(self.old)
        self.new = self.docker.build("new")
        self.clock = 0.0
        self.said[:] = []

    def test_it_waits_while_a_job_is_in_production(self):
        self.docker.jobs = [1, 1, 1, 0]
        self.assertEqual(self.run_deploy(self.new), deploy.EXIT_OK)
        self.assertEqual(len(self.docker.ran("exec")), 4)
        self.assertTrue(any("waiting up to 10 min" in line for line in self.said))
        self.assertEqual(self.box()["labels"][deploy.LABEL_IMAGE], self.new)

    def test_it_goes_ahead_when_the_wait_is_used_up(self):
        self.docker.jobs = [2]
        self.assertEqual(self.run_deploy(self.new), deploy.EXIT_OK)
        self.assertTrue(any("going ahead" in line for line in self.said))
        self.assertGreaterEqual(self.clock, 600)
        self.assertEqual(self.box()["labels"][deploy.LABEL_IMAGE], self.new)

    def test_no_wait_when_the_spec_says_zero(self):
        self.write_spec(quiet_wait=0)
        self.docker.jobs = [3]
        self.assertEqual(self.run_deploy(self.new), deploy.EXIT_OK)
        self.assertEqual(self.docker.ran("exec"), [])

    def test_an_engine_that_does_not_say_is_not_waited_for(self):
        self.docker.jobs = []  # the question fails
        self.assertEqual(self.run_deploy(self.new), deploy.EXIT_OK)
        self.assertLess(self.clock, 60)


class Leftovers(DeployCase):
    def test_a_run_that_died_after_setting_the_old_one_aside_is_put_right(self):
        old = self.docker.build("old")
        self.run_deploy(old)
        self.docker.containers[self.NAME + "-previous"] = self.docker.containers.pop(self.NAME)
        self.docker.containers[self.NAME + "-previous"]["running"] = False
        new = self.docker.build("new")
        self.assertEqual(self.run_deploy(new), deploy.EXIT_OK)
        self.assertTrue(any("stopped half-way" in line for line in self.said))
        self.assertEqual(self.box()["labels"][deploy.LABEL_IMAGE], new)
        self.assertIsNone(self.box(self.NAME + "-previous"))

    def test_a_run_that_died_after_a_good_start_only_leaves_a_leftover(self):
        old = self.docker.build("old")
        self.run_deploy(old)
        new = self.docker.build("new")
        self.docker.containers[self.NAME + "-previous"] = dict(self.docker.containers[self.NAME], running=False)
        self.docker.containers[self.NAME] = dict(self.docker.containers[self.NAME],
                                                 image=self.docker.image(new)["id"], labels={})
        self.docker.calls[:] = []
        self.assertEqual(self.run_deploy(new), deploy.EXIT_OK)
        self.assertTrue(any("leftover" in line for line in self.said))
        self.assertIsNone(self.box(self.NAME + "-previous"))


class Images(DeployCase):
    def test_only_this_engines_older_builds_are_deleted(self):
        first, second, third = (self.docker.build(name, pulled=False) for name in ("first", "second", "third"))
        neighbour = self.docker.build("neighbour", repository=OTHER)
        for build in (first, second, third):
            self.assertEqual(self.run_deploy(build), deploy.EXIT_OK)
            self.assertIsNotNone(self.docker.image(neighbour))
        self.assertIsNone(self.docker.image(first))
        self.assertIsNotNone(self.docker.image(second))   # the previous one stays, for going back
        self.assertIsNotNone(self.docker.image(third))
        self.assertIsNotNone(self.docker.image(neighbour))
        for call in self.docker.ran("rmi"):
            self.assertTrue(call[-1].startswith(REPOSITORY + "@"))
            self.assertNotIn("-f", call)
        self.assertTrue(any("removed 1 older build" in line for line in self.said))

    def test_a_failed_build_is_kept_until_the_next_good_one(self):
        good = self.docker.build("good")
        self.run_deploy(good)
        bad = self.docker.build("bad", ready=False)
        self.run_deploy(bad)
        self.assertIsNotNone(self.docker.image(bad))
        self.assertIsNotNone(self.docker.image(good))


class Refusals(DeployCase):
    def refused(self, image=None, **changes):
        self.write_spec(**changes)
        code = self.run_deploy(image or self.docker.build("one"))
        self.assertEqual(code, deploy.EXIT_REFUSED)
        for word in ("run", "stop", "rename", "pull", "rm", "rmi"):
            self.assertEqual(self.docker.ran(word), [])
        self.assert_nothing_private_was_said()

    def test_an_image_of_another_repository(self):
        self.refused(image=self.docker.build("other", repository=OTHER))

    def test_an_image_reference_with_more_in_it(self):
        self.refused(image=REPOSITORY + "@sha256:" + "0" * 64 + " --privileged")
        self.refused(image=REPOSITORY + ":tag;reboot")

    def test_a_spec_with_unknown_settings(self):
        self.refused(extra_arguments="--privileged")

    def test_values_that_are_not_what_they_claim(self):
        for change in ({"name": "x; reboot"}, {"env_file": "engine.env"}, {"env_file": "/nowhere/engine.env"},
                       {"publish": ["0.0.0.0:99999"]}, {"publish": []}, {"publish": "127.0.0.1:8710"},
                       {"cpuset": "0-3 --privileged"}, {"memory": "lots"}, {"volume": "../etc"},
                       {"volume": "/data/../etc"}, {"stop_timeout": "40"}, {"ready_timeout": 5},
                       {"repository": "ghcr.io/Example/Api"}, {"probe": "localhost:8710"}):
            settings = dict(self.settings)
            self.refused(**change)
            self.settings = settings

    def test_a_spec_that_is_not_json(self):
        self.spec_file.write_text("repository=...", encoding="utf-8")
        self.assertEqual(self.run_deploy(REPOSITORY + ":tag"), deploy.EXIT_REFUSED)

    def test_a_tag_of_the_repository_is_accepted(self):
        build = self.docker.build("one")
        self.docker.image(build)["references"].add(REPOSITORY + ":sha-0a1b2c3")
        self.assertEqual(self.run_deploy(REPOSITORY + ":sha-0a1b2c3"), deploy.EXIT_OK)


class OneAtATime(DeployCase):
    def test_a_second_run_is_turned_away(self):
        holder = deploy.take_lock(self.folder)
        self.addCleanup(holder.close)
        self.assertEqual(self.run_deploy(self.docker.build("one")), deploy.EXIT_BUSY)
        self.assertEqual(self.docker.calls, [])


class Records(DeployCase):
    def test_old_release_folders_are_pruned(self):
        self.write_spec(keep_releases=3)
        build = self.docker.build("one")
        for _ in range(6):
            self.assertEqual(self.run_deploy(build), deploy.EXIT_OK)
        self.assertEqual(len(self.releases()), 3)

    def test_a_given_release_folder_is_used(self):
        release = self.folder / "releases" / "20260101T000000Z-abcdef"
        release.mkdir(parents=True)
        self.assertEqual(self.run_deploy(self.docker.build("one"), release=release), deploy.EXIT_OK)
        self.assertTrue((release / "record.json").is_file())
        self.assertTrue((release / "seccomp.json").is_file())


class Words(unittest.TestCase):
    def test_a_docker_refusal_names_the_operation_only(self):
        original = deploy._run
        self.addCleanup(setattr, deploy, "_run", original)
        deploy._run = lambda argv, timeout: deploy.Answer(1, "", "invalid env " + SECRET)
        with self.assertRaises(deploy.DeployError) as caught:
            deploy.docker("run", "--env-file", "/opt/x/engine.env", "image")
        self.assertEqual(str(caught.exception), "Docker refused 'run'")

    def test_the_self_check_passes(self):
        said = []
        original = deploy.say
        self.addCleanup(setattr, deploy, "say", original)
        deploy.say = said.append
        self.assertEqual(deploy.main(["--self-check"]), deploy.EXIT_OK)
        self.assertTrue(said)


if __name__ == "__main__":
    unittest.main()
