import json
import os
import shutil
import subprocess
import time
import unittest
from unittest import mock

from support import ROOT, HomeCase, hook_input, join, new_sid, post
from passnote import __version__, doctor, hook, paths, trust

PLUGIN = os.path.join(ROOT, "plugin")
REPO_URL = "https://github.com/ploono/passnote"
GUARD = os.path.join(PLUGIN, "hooks", "guard.sh")
EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "PostToolBatch", "PreToolUse")
MATCHERS = {"SessionStart": "startup|resume|clear|compact|fork", "SessionEnd": "clear", "PreToolUse": "Bash"}
# The guard must behave the same under every POSIX sh it meets: macOS's /bin/sh is bash in sh
# mode, Debian/Ubuntu's is dash (macOS ships /bin/dash too).
SHELLS = tuple(sh for sh in ("/bin/sh", "/bin/dash") if os.path.exists(sh))
STUB = "#!/bin/sh\nprintf 'python3 %s\\n' \"$*\"\ncat\n"


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def frontmatter(text):
    """The top-level `key: value` lines between the leading --- lines."""
    head = text.split("---\n", 2)[1]
    return dict(line.split(": ", 1) for line in head.splitlines() if ": " in line and not line.startswith(" "))


def tracked():
    """{path: mode} from `git ls-files -s`, or None outside a git checkout."""
    git = shutil.which("git")
    if git is None or not os.path.exists(os.path.join(ROOT, ".git")):
        return None
    res = subprocess.run([git, "-C", ROOT, "ls-files", "-s", "-z"], capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        return None
    entries = {}
    for record in res.stdout.split("\0"):
        if record:
            meta, path = record.split("\t", 1)
            entries[path] = meta.split()[0]
    return entries


class ManifestTest(unittest.TestCase):
    def test_hooks_json(self):
        hooks = load(os.path.join(PLUGIN, "hooks", "hooks.json"))["hooks"]
        self.assertEqual(set(hooks), set(EVENTS))
        for event, groups in hooks.items():
            with self.subTest(event=event):
                self.assertEqual(len(groups), 1)
                self.assertEqual(groups[0].get("matcher"), MATCHERS.get(event))
                # Exec form: no shell parses the plugin path, so spaces and quotes in it are safe.
                self.assertEqual(groups[0]["hooks"], [{
                    "type": "command",
                    "command": "/bin/sh",
                    "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/guard.sh", event],
                    "timeout": 5,
                }])

    def test_plugin_manifest(self):
        plugin = load(os.path.join(PLUGIN, ".claude-plugin", "plugin.json"))
        self.assertEqual(plugin["name"], "passnote")
        self.assertEqual(plugin["version"], __version__)  # the one version, in two places
        self.assertEqual(plugin["author"], {"name": "Nazar Kuzmenko"})
        self.assertEqual((plugin["homepage"], plugin["repository"]), (REPO_URL, REPO_URL))
        self.assertEqual(plugin["license"], "MIT")
        self.assertIn("messaging", plugin["keywords"])
        self.assertTrue(plugin["description"])

    def test_marketplace(self):
        market = load(os.path.join(ROOT, ".claude-plugin", "marketplace.json"))
        # The README's `passnote@passnote` is <plugin>@<marketplace>.
        self.assertEqual(market["name"], "passnote")
        self.assertEqual(market["owner"], {"name": "Nazar Kuzmenko"})
        self.assertTrue(market["description"])  # without it `claude plugin validate --strict` fails
        (entry,) = market["plugins"]
        self.assertEqual((entry["name"], entry["source"]), ("passnote", "./plugin"))
        # plugin.json's version wins at install time; a second copy could only drift.
        self.assertNotIn("version", entry)

    def test_skill_frontmatter(self):
        text = read(os.path.join(PLUGIN, "skills", "passnote", "SKILL.md"))
        self.assertTrue(text.startswith("---\nname: passnote\ndescription: "))
        fields = frontmatter(text)
        # description + when_to_use is what every session pays for, joined or not.
        self.assertLessEqual(len(fields["description"]) + len(fields.get("when_to_use", "")), 250)
        # A grant would pre-approve `passnote post` while the skill is active; peers can't approve actions.
        self.assertNotIn("allowed-tools", fields)

    def test_skill_uses_the_glossary(self):
        text = read(os.path.join(PLUGIN, "skills", "passnote", "SKILL.md"))
        self.assertIn("`WAIT <name>", text)
        for old in ("QUEUED", "pending", "idle 7"):
            self.assertNotIn(old, text)

    def test_readme(self):
        text = read(os.path.join(ROOT, "README.md"))
        self.assertIn(doctor.README_SNIPPET, text)  # what doctor's fix line tells users to paste
        self.assertIn("/plugin marketplace add ploono/passnote", text)
        self.assertIn("claude plugin install passnote@passnote", text)
        self.assertNotIn("<owner>", text)

    def test_entrypoints_are_executable(self):
        for path in (GUARD, os.path.join(PLUGIN, "bin", "passnote")):
            self.assertTrue(os.access(path, os.X_OK), path)

    def test_committed_entrypoints_are_executable(self):
        modes = tracked()
        if modes is None:
            self.skipTest("not a git checkout")
        for path in ("plugin/hooks/guard.sh", "plugin/bin/passnote"):
            self.assertEqual(modes.get(path), "100755", path)


class HygieneTest(unittest.TestCase):
    def test_no_tracked_file_holds_a_secret_shaped_literal(self):
        """The repo is public, and GitHub push protection scans history: test fixtures build these
        shapes at runtime. Generic KEY=value assignments are not provider tokens and are allowed."""
        modes = tracked()
        if modes is None:
            self.skipTest("not a git checkout")
        patterns = [(label, pattern) for label, pattern in trust.SECRET_PATTERNS if label != "credential assignment"]
        found = []
        for path in modes:
            try:
                with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            found += [f"{path}: {label}" for label, pattern in patterns if pattern.search(text)]
        self.assertEqual(found, [])


class GuardTest(HomeCase):
    def setUp(self):
        super().setUp()
        self.cwd = os.path.join(self.tmp, "cwd")
        os.makedirs(self.cwd)
        os.makedirs(os.environ["HOME"])
        self.pid = str(os.getpid())

    def run_guard(self, guard, event, sid, stdin, shell="/bin/sh", env=None, **env_extra):
        env = self.env(sid, **env_extra) if env is None else env
        start = time.monotonic()
        argv = [guard, event] if shell is None else [shell, guard, event]
        res = subprocess.run(argv, input=stdin, env=env, cwd=self.cwd, capture_output=True, text=True, timeout=30)
        return res, time.monotonic() - start

    def stub_path(self):
        """A PATH whose python3 prints its argv and then its stdin: shows whether the guard let a
        fire through to Python, with what, without running passnote."""
        bindir = os.path.join(self.tmp, "stub")
        if not os.path.isdir(bindir):
            os.makedirs(bindir)
            stub = os.path.join(bindir, "python3")
            with open(stub, "w", encoding="utf-8") as fh:
                fh.write(STUB)
            os.chmod(stub, 0o755)
        return bindir + os.pathsep + os.environ.get("PATH", os.defpath)

    def reached(self, event, sid, stdin="{}", env=None, **env_extra):
        """The stub's output (empty when the guard stopped the fire), the same under every shell."""
        if env is None:
            env = self.env(sid, PATH=self.stub_path(), **env_extra)
        outputs = set()
        for shell in SHELLS:
            res, _ = self.run_guard(GUARD, event, sid, stdin, shell=shell, env=env)
            self.assertEqual((res.returncode, res.stderr), (0, ""), shell)
            outputs.add(res.stdout)
        self.assertEqual(len(outputs), 1, outputs)
        return outputs.pop()

    def mark_joined(self, home, sid):
        os.makedirs(os.path.join(home, "sessions", sid), exist_ok=True)
        with open(os.path.join(home, "sessions", sid, "joined"), "w", encoding="utf-8"):
            pass

    def write_by_pid(self, record):
        path = os.path.join(self.home, "sessions", "by-pid", f"{self.pid}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        paths.atomic_write_json(path, record)

    def test_unjoined_session_exits_fast_and_silent(self):
        res, elapsed = self.run_guard(GUARD, "PostToolBatch", new_sid(), "{}")
        self.assertEqual((res.returncode, res.stdout), (0, ""))
        self.assertLess(elapsed, 1.0)

    def test_unjoined_session_never_reaches_python(self):
        sid = new_sid()
        for event in ("SessionStart", "SessionEnd", "UserPromptSubmit", "PostToolBatch"):
            with self.subTest(event=event):
                self.assertEqual(self.reached(event, sid, hook_input(sid, event=event), CLAUDE_PID=self.pid), "")

    def test_joined_session_reaches_python_on_every_event_but_unknown_ones(self):
        sid = new_sid()
        self.mark_joined(self.home, sid)
        for event in ("SessionStart", "SessionEnd", "UserPromptSubmit", "PostToolBatch"):
            with self.subTest(event=event):
                out = self.reached(event, sid)
                self.assertTrue(out.startswith("python3 -I "), out)
                self.assertTrue(out.endswith(f"/bin/passnote hook {event}\n{{}}"), out)
        self.assertEqual(self.reached("Stop", sid), "")

    def test_guard_handles_spaces_in_paths(self):
        os.environ["PASSNOTE_HOME"] = os.path.join(self.tmp, "state dir", "passnote")
        paths.ensure_home()
        plugin_copy = os.path.join(self.tmp, "plug in")
        shutil.copytree(PLUGIN, plugin_copy)
        a, b = new_sid(), new_sid()
        join(a, "r", "alice")
        join(b, "r", "bob")
        guard = os.path.join(plugin_copy, "hooks", "guard.sh")
        post(a, "r", "through the guard")
        res, _ = self.run_guard(guard, "PostToolBatch", b, hook_input(b))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("through the guard", res.stdout)
        post(a, "r", "run directly")  # by its shebang, as an exec-form hook could run it
        res, _ = self.run_guard(guard, "PostToolBatch", b, hook_input(b), shell=None)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("run directly", res.stdout)

    def test_guard_exits_zero_without_python(self):
        paths.ensure_home()
        a, b = new_sid(), new_sid()
        join(a, "r", "alice")
        join(b, "r", "bob")
        post(a, "r", "x")
        bindir = os.path.join(self.tmp, "nopython")
        os.makedirs(bindir)
        os.symlink(shutil.which("uname"), os.path.join(bindir, "uname"))
        res, _ = self.run_guard(GUARD, "PostToolBatch", b, hook_input(b), PATH=bindir)
        self.assertEqual((res.returncode, res.stdout), (0, ""))

    def test_guard_exits_silently_on_an_unsupported_os(self):
        sid = new_sid()
        self.mark_joined(self.home, sid)
        bindir = os.path.join(self.tmp, "freebsd")
        os.makedirs(bindir)
        uname = os.path.join(bindir, "uname")
        with open(uname, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho FreeBSD\n")
        os.chmod(uname, 0o755)
        # The stub python3 comes after the fake uname on PATH: it must not be reached.
        path = bindir + os.pathsep + self.stub_path()
        self.assertNotEqual(self.reached("PostToolBatch", sid), "")  # the real uname: reached
        self.assertEqual(self.reached("PostToolBatch", sid, env=self.env(sid, PATH=path)), "")

    def test_home_resolution_matches_python(self):
        """The guard finds the joined marker exactly where paths.home() puts it, and never at the
        place a naive reading of the variables would (a relative value is ignored, ~/ expands)."""
        user_home = os.environ["HOME"]
        variants = [
            ({"PASSNOTE_HOME": "~/x"}, os.path.join(self.cwd, "~", "x")),
            ({"PASSNOTE_HOME": "~"}, None),
            ({"PASSNOTE_HOME": os.path.join(self.tmp, "abs")}, None),
            ({"PASSNOTE_HOME": "rel/home", "XDG_STATE_HOME": os.path.join(self.tmp, "xdg")},
             os.path.join(self.cwd, "rel", "home")),
            ({"PASSNOTE_HOME": "", "XDG_STATE_HOME": "~/state"}, os.path.join(self.cwd, "~", "state", "passnote")),
            ({"XDG_STATE_HOME": "relxdg"}, os.path.join(self.cwd, "relxdg", "passnote")),
            ({}, None),
        ]
        sid = new_sid()
        for variant, decoy in variants:
            with self.subTest(variant=variant):
                env = {k: v for k, v in os.environ.items() if k not in ("PASSNOTE_HOME", "XDG_STATE_HOME")}
                env.update(variant, CLAUDE_CODE_SESSION_ID=sid, PATH=self.stub_path())
                with mock.patch.dict(os.environ, env, clear=True):
                    expected = paths.home()
                self.assertTrue(expected.startswith((user_home, self.tmp)), expected)
                self.mark_joined(expected, sid)
                self.assertNotEqual(self.reached("PostToolBatch", sid, env=env), "")
                shutil.rmtree(os.path.join(expected, "sessions", sid))
                self.assertEqual(self.reached("PostToolBatch", sid, env=env), "")
                if decoy:
                    self.mark_joined(decoy, sid)
                    self.assertEqual(self.reached("PostToolBatch", sid, env=env), "")
                    shutil.rmtree(os.path.join(decoy, "sessions", sid))

    def test_a_tilde_form_only_python_can_expand_is_left_to_python(self):
        env = self.env(new_sid(), PATH=self.stub_path(), PASSNOTE_HOME="~nosuchuser/x")
        self.assertNotEqual(self.reached("PostToolBatch", env["CLAUDE_CODE_SESSION_ID"], env=env), "")

    def test_session_start_and_end_pass_with_a_pid_record(self):
        # A resume runs in a new process; a /clear's new sid has joined nothing yet.
        sid = new_sid()
        self.write_by_pid({"sid": new_sid(), "pid_started_at": "t"})
        # Python ignores a CLAUDE_PID that isn't digits (sessions.parse_pid), so the guard does too.
        garbled = os.path.join(self.home, "sessions", "by-pid", "1x.json")
        paths.atomic_write_json(garbled, {"sid": new_sid(), "pid_started_at": "t", "prev_sid": new_sid()})
        for event in ("SessionStart", "SessionEnd", "PostToolBatch"):
            with self.subTest(event=event):
                self.assertEqual(self.reached(event, sid, CLAUDE_PID="1x"), "")
        for event in ("SessionStart", "SessionEnd"):
            with self.subTest(event=event):
                self.assertNotEqual(self.reached(event, sid, CLAUDE_PID=self.pid), "")
                self.assertEqual(self.reached(event, sid, CLAUDE_PID="1"), "")
        # Delivery events read the record: only an unfinished /clear carry from another sid passes.
        self.assertEqual(self.reached("PostToolBatch", sid, CLAUDE_PID=self.pid), "")
        self.write_by_pid({"sid": sid, "pid_started_at": "t", "prev_sid": sid})
        self.assertEqual(self.reached("UserPromptSubmit", sid, CLAUDE_PID=self.pid), "")
        self.write_by_pid({"sid": sid, "pid_started_at": "t", "prev_sid": new_sid()})
        for event in ("UserPromptSubmit", "PostToolBatch"):
            self.assertNotEqual(self.reached(event, sid, CLAUDE_PID=self.pid), "")

    def test_delivery_finishes_an_unfinished_clear_carry(self):
        paths.ensure_home()
        a, b, new = new_sid(), new_sid(), new_sid()
        join(a, "r", "alice")
        join(b, "r", "bob")
        # SessionEnd(clear) recorded the old sid; SessionStart(clear) never finished the carry.
        hook.main("SessionEnd", json.dumps({"session_id": b, "reason": "clear"}), self.env(b, CLAUDE_PID=self.pid))
        post(a, "r", "after the clear")
        res, _ = self.run_guard(GUARD, "PostToolBatch", new, hook_input(new), CLAUDE_PID=self.pid)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("after the clear", res.stdout)

    def test_pre_tool_use_reaches_python_only_for_a_subagent_passnote_command(self):
        sid = new_sid()  # unjoined: a subagent must not join as its parent
        command = {"command": "printf 'a\\nb' | passnote join --as bob"}
        stdin = hook_input(sid, event="PreToolUse", agent_id="sub-1", tool_name="Bash", tool_input=command)
        out = self.reached("PreToolUse", sid, stdin)
        first, forwarded = out.split("\n", 1)
        self.assertTrue(first.endswith("/bin/passnote hook PreToolUse"), first)
        self.assertEqual(forwarded, stdin)  # passed on unchanged
        self.assertEqual(self.reached("PreToolUse", sid, stdin + "\n").split("\n", 1)[1], stdin + "\n")
        no_agent = hook_input(sid, event="PreToolUse", tool_name="Bash", tool_input=command)
        self.assertEqual(self.reached("PreToolUse", sid, no_agent), "")
        other = hook_input(sid, event="PreToolUse", agent_id="sub-1", tool_name="Bash", tool_input={"command": "ls"})
        self.assertEqual(self.reached("PreToolUse", sid, other), "")
        # "passnote" only in fields before the command (paths of a passnote checkout) doesn't count.
        elsewhere = hook_input(sid, event="PreToolUse", agent_id="sub-1", tool_name="Bash", cwd="/src/passnote",
                               transcript_path="/x/passnote.jsonl", tool_input={"command": "ls"})
        self.assertLess(elsewhere.index("passnote"), elsewhere.index('"command"'))
        self.assertEqual(self.reached("PreToolUse", sid, elsewhere), "")

    def test_pre_tool_use_denies_a_subagent_of_an_unjoined_parent(self):
        sid = new_sid()
        stdin = hook_input(sid, event="PreToolUse", agent_id="sub-1", tool_name="Bash",
                           tool_input={"command": "passnote join --as bob"})
        res, _ = self.run_guard(GUARD, "PreToolUse", sid, stdin)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
