"""docker/deploy/server-entry.sh: the one command a pipeline's key may run on a host.

It is run here as the host runs it, with a stand-in ``docker`` on the PATH. Kept to unittest and to
Python 3.6, like the helper's own tests (the pipeline runs this folder under 3.6 too).
"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "docker" / "deploy" / "server-entry.sh"
REPOSITORY = "ghcr.io/example/openmontage-api"
DIGEST = "sha256:" + "0123456789abcdef" * 4

# What stands in for docker: it writes every call down, and its "cp" leaves a helper that prints
# what it was started with.
DOCKER = r"""#!/bin/sh
echo "$*" >> "$STUB/calls"
case "$1" in
  pull) [ -e "$STUB/refuse-pull" ] && { echo "pull error with a secret"; exit 1; }; echo "pulled a layer" ;;
  create) echo carrier1 ;;
  cp) [ -e "$STUB/refuse-cp" ] && { echo "no such file" >&2; exit 1; }
      printf '%s\n' 'import os, sys' 'print("HELPER " + " ".join(sys.argv[1:]))' \
        'sys.exit(int(os.environ.get("STUB_HELPER_EXIT", "0")))' > "$3" ;;
  rm) : ;;
esac
"""


@unittest.skipIf(os.name == "nt", "the entry script serves POSIX Docker hosts")
class EntryCase(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.folder), True)
        self.host = self.folder / "host"
        self.stub = self.folder / "stub"
        for folder in (self.host, self.stub, self.folder / "bin"):
            folder.mkdir()
        self.entry = self.host / "deploy"
        shutil.copyfile(str(ENTRY), str(self.entry))
        docker = self.folder / "bin" / "docker"
        docker.write_text(DOCKER, encoding="utf-8")
        for script in (self.entry, docker):
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
        self.write_spec('{"repository": "%s"}' % REPOSITORY)

    def write_spec(self, text):
        (self.host / "deploy.json").write_text(text, encoding="utf-8")

    def ask(self, request, helper_exit=0, as_arguments=False):
        env = dict(os.environ, PATH=str(self.folder / "bin") + os.pathsep + os.environ["PATH"],
                   STUB=str(self.stub), STUB_HELPER_EXIT=str(helper_exit))
        env.pop("SSH_ORIGINAL_COMMAND", None)
        command = [str(self.entry)]
        if as_arguments:
            command += request.split(" ")
        else:
            env["SSH_ORIGINAL_COMMAND"] = request
        done = subprocess.run(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=60)
        return done.returncode, done.stdout

    def calls(self):
        log = self.stub / "calls"
        return log.read_text(encoding="utf-8").splitlines() if log.is_file() else []

    def test_a_digest_is_pulled_and_handed_to_the_helper_from_that_image(self):
        code, said = self.ask("deploy " + DIGEST)
        self.assertEqual(code, 0, said)
        image = "%s@%s" % (REPOSITORY, DIGEST)
        calls = self.calls()
        self.assertEqual(calls[0], "pull -q " + image)
        self.assertEqual(calls[1], "create --entrypoint /bin/true " + image)
        self.assertTrue(calls[2].startswith("cp carrier1:/app/docker/deploy/deploy.py " + str(self.host / "releases")))
        self.assertEqual(calls[3], "rm -v carrier1")  # the carrier never stays behind
        helper = [line for line in said.splitlines() if line.startswith("HELPER ")][0].split(" ")
        self.assertEqual(helper[1:5], ["--spec", str(self.host / "deploy.json"), "--image", image])
        self.assertEqual(helper[5], "--release")
        release = Path(helper[6])
        self.assertEqual(release.parent, self.host / "releases")
        self.assertRegex(release.name, r"^\d{8}T\d{6}Z-[A-Za-z0-9]{6}$")
        self.assertEqual(stat.S_IMODE(release.stat().st_mode), 0o700)
        self.assertNotIn("pulled a layer", said)  # nothing Docker printed is passed on

    def test_the_helpers_result_is_the_result(self):
        for wanted in (1, 2, 75):
            code, _ = self.ask("deploy " + DIGEST, helper_exit=wanted)
            self.assertEqual(code, wanted)

    def test_it_can_be_run_by_hand_with_the_request_as_arguments(self):
        code, said = self.ask("deploy " + DIGEST, as_arguments=True)
        self.assertEqual(code, 0, said)
        self.assertIn("HELPER ", said)

    def test_anything_but_one_digest_is_refused_before_docker_is_touched(self):
        for request in ("", "deploy", "deploy " + DIGEST[:-1], "deploy " + DIGEST + "0", "deploy " + DIGEST.upper(),
                        "deploy " + DIGEST + "; reboot", "deploy " + DIGEST + " --privileged",
                        "deploy sha-0a1b2c3", "deploy latest", "sh", "rm -rf /", "scp -t /etc",
                        "deploy " + DIGEST + "\ndeploy " + DIGEST, "undeploy " + DIGEST, " deploy " + DIGEST):
            code, said = self.ask(request)
            self.assertEqual(code, 64, repr(request))
            self.assertIn("refused", said)
            self.assertEqual(self.calls(), [], repr(request))

    def test_a_host_without_a_usable_spec_refuses(self):
        for text in ("", "not json", "{}", '{"repository": "ghcr.io/x/y; reboot"}', '{"repository": "UPPER/case"}',
                     '{"repository": 5}'):
            self.write_spec(text)
            code, said = self.ask("deploy " + DIGEST)
            self.assertEqual(code, 64, text)
            self.assertNotIn(str(self.host), said)  # no path of the host in a log that may be public
            self.assertEqual(self.calls(), [], text)

    def test_a_build_that_cannot_be_pulled_stops_there(self):
        (self.stub / "refuse-pull").write_text("", encoding="utf-8")
        code, said = self.ask("deploy " + DIGEST)
        self.assertEqual(code, 1)
        self.assertIn("could not be pulled", said)
        self.assertNotIn("secret", said)
        self.assertNotIn("HELPER", said)
        self.assertEqual(len(self.calls()), 1)

    def test_a_build_without_the_helper_stops_there_and_leaves_no_carrier(self):
        (self.stub / "refuse-cp").write_text("", encoding="utf-8")
        code, said = self.ask("deploy " + DIGEST)
        self.assertEqual(code, 1)
        self.assertIn("no deploy helper", said)
        self.assertEqual(self.calls()[-1], "rm -v carrier1")


if __name__ == "__main__":
    unittest.main()
