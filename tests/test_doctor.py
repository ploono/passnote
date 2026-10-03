import io
import json
import os
import subprocess
import time
import unittest
from unittest import mock

from support import CliCase
from passnote import doctor, paths, sessions

MIN = ".".join(map(str, doctor.MIN_CLAUDE))


def runner_for(version_text):
    def fake(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=version_text, stderr="")
    return fake


def on_path(name):
    return "/synthetic/bin/" + name


def not_on_path(name):
    return None


def run_doctor(env, version=MIN + " (Claude Code)", which=on_path):
    out = io.StringIO()
    code = doctor.run(env, out, runner=runner_for(version), which=which)
    return code, out.getvalue()


class DoctorTest(CliCase):
    def setUp(self):
        super().setUp()
        # CLI runs must never find (or run) a real `claude`.
        patcher = mock.patch("passnote.doctor.shutil.which", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def results(self, env, version=MIN + " (Claude Code)", which=on_path, now=None):
        return doctor.checks(env, runner=runner_for(version), which=which, now=now)

    def statuses(self, env, version=MIN + " (Claude Code)", **kwargs):
        return {label: status for status, label, _ in self.results(env, version, **kwargs)}

    def test_version_gate(self):
        env = self.env(self.a)
        self.assertEqual(self.statuses(env)["Claude Code " + MIN], "ok")
        self.assertEqual(self.statuses(env, "2.1.200 (Claude Code)")["Claude Code 2.1.200 is older than " + MIN], "fail")

    def test_unparsable_version_warns(self):
        self.assertEqual(self.statuses(self.env(self.a), "no digits here")["Claude Code version unknown"], "warn")

    def test_claude_missing_from_path_does_not_run_the_runner(self):
        def boom(cmd, **kwargs):
            raise AssertionError("the runner must not be called")
        results = doctor.checks(self.env(self.a), runner=boom, which=not_on_path)
        entry = [r for r in results if r[1].startswith("claude is not on PATH")]
        self.assertEqual(entry[0][0], "warn")
        self.assertIn("PATH", entry[0][2])

    def test_runner_failure_is_unknown_version(self):
        def broken(cmd, **kwargs):
            raise OSError("gone")
        results = doctor.checks(self.env(self.a), runner=broken, which=on_path)
        self.assertIn(("warn", "Claude Code version unknown"), [(s, label) for s, label, _ in results])

    def test_hooks_disabled_fails(self):
        with open(os.path.join(self.claude_home, "settings.json"), "w") as fh:
            json.dump({"disableAllHooks": True}, fh)
        self.assertEqual(self.statuses(self.env(self.a))["disableAllHooks is set"], "fail")

    def test_session_checks(self):
        self.run_cli(self.a, "join", "r", "--as", "alice")
        self.run_cli(self.b, "join", "r", "--as", "bob")
        for sid, mode in ((self.a, "default"), (self.b, "bypassPermissions")):
            meta = sessions.load_meta(sid)
            meta["permission_mode"] = mode
            sessions.save_meta(sid, meta)
        results = self.results(self.env(self.a))
        statuses = {label: status for status, label, _ in results}
        self.assertEqual(statuses["the passnote hook has not fired in this session recently"], "warn")
        self.assertEqual(statuses["bob in r is in a different permission class"], "warn")
        fix = [f for s, label, f in results if "different permission class" in label][0]
        self.assertIn("human", fix)
        sessions.touch_active(self.a)
        self.assertEqual(self.statuses(self.env(self.a))["hook fired in this session"], "ok")

    def class_warnings(self, modes):
        for sid, name in ((self.a, "alice"), (self.b, "bob"), (self.c, "carol")):
            self.run_cli(sid, "join", "r", "--as", name)
        for sid, mode in modes:
            sessions.update_meta(sid, lambda meta, mode=mode: meta.update(permission_mode=mode))
        return [label for label in self.statuses(self.env(self.a)) if "different permission class" in label]

    def test_no_class_warning_before_this_sessions_mode_is_recorded(self):
        self.assertEqual(self.class_warnings([(self.b, "bypassPermissions"), (self.c, "default")]), [])

    def test_no_class_warning_for_a_peer_whose_mode_is_not_recorded(self):
        # carol's mode is unknown: no warning for her; bob's is known and differs
        self.assertEqual(self.class_warnings([(self.a, "default"), (self.b, "auto")]),
                         ["bob in r is in a different permission class"])

    def test_hook_fire_older_than_the_session_ttl_is_not_recent(self):
        self.run_cli(self.a, "join", "r", "--as", "alice")
        now = time.time()
        sessions.touch_active(self.a, now - sessions.DEFAULT_TTL - 5)
        self.assertEqual(
            self.statuses(self.env(self.a), now=now)["the passnote hook has not fired in this session recently"], "warn")
        sessions.touch_active(self.a, now - 5)
        self.assertEqual(self.statuses(self.env(self.a), now=now)["hook fired in this session"], "ok")

    def test_not_joined_and_bad_session_id_warn(self):
        self.assertEqual(self.statuses(self.env(self.a))["this session has not joined a room"], "warn")
        statuses = self.statuses(self.env("not-a-uuid"))
        self.assertEqual(statuses["CLAUDE_CODE_SESSION_ID is not a valid session id"], "warn")

    def test_no_session_id_skips_session_checks(self):
        env = {k: v for k, v in self.env(self.a).items() if k != "CLAUDE_CODE_SESSION_ID"}
        labels = list(self.statuses(env))
        self.assertFalse([label for label in labels if "session" in label or "hook fired" in label])

    def test_cross_session_inbound_is_reported(self):
        with open(os.path.join(self.claude_home, "settings.json"), "w") as fh:
            json.dump({"crossSessionInbound": "accept"}, fh)
        self.assertEqual(self.statuses(self.env(self.a))["crossSessionInbound = accept"], "ok")

    def test_recent_errors_warn_old_ones_do_not(self):
        os.makedirs(self.home, mode=0o700)
        now = time.time()
        with open(os.path.join(self.home, "errors.log"), "w") as fh:
            fh.write(json.dumps({"ts": now - 2 * 86400, "where": "old", "error": "OSError"}) + "\n")
            fh.write("not json\n")
            fh.write(json.dumps({"ts": now - 60, "where": "hook", "error": "KeyError", "note": "synthetic"}) + "\n")
        results = self.results(self.env(self.a), now=now)
        entry = [r for r in results if "error(s) in the last 24h" in r[1]]
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0][0], "warn")
        self.assertIn("1 error(s)", entry[0][1])
        self.assertIn("hook: KeyError: synthetic", entry[0][1])
        self.assertNotIn("old", entry[0][1])

    def test_unwritable_storage_fails_with_sandbox_fix(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory modes")
        os.makedirs(self.home, exist_ok=True)
        os.chmod(self.home, 0o500)
        self.addCleanup(os.chmod, self.home, 0o700)
        code, text = run_doctor(self.env(self.a))
        self.assertEqual(code, 1)
        self.assertIn("allowWrite", text)

    def test_uncreatable_home_is_a_failing_line_not_an_abort(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory modes")
        locked = os.path.join(self.tmp, "locked")
        os.makedirs(locked, mode=0o700)
        os.chmod(locked, 0o500)
        self.addCleanup(os.chmod, locked, 0o700)
        os.environ["PASSNOTE_HOME"] = os.path.join(locked, "home")
        code, out, err = self.run_cli(self.a, "doctor")
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertIn("allowWrite", out)
        self.assertIn(os.path.join(locked, "home"), out)
        self.assertNotIn("Traceback", out)

    def test_readme_snippet_only_for_the_default_home(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory modes")
        state = os.path.join(self.tmp, "state")
        os.makedirs(state, mode=0o700)
        os.chmod(state, 0o500)
        self.addCleanup(os.chmod, state, 0o700)
        del os.environ["PASSNOTE_HOME"]
        os.environ["XDG_STATE_HOME"] = state
        self.assertEqual(paths.home(), os.path.join(state, "passnote"))
        _, out, _ = self.run_cli(self.a, "doctor")
        self.assertIn("allowWrite", out)
        self.assertNotIn('{"sandbox"', out)
        del os.environ["XDG_STATE_HOME"]
        os.environ["HOME"] = os.path.join(self.tmp, "user-home")
        os.makedirs(os.environ["HOME"])
        os.chmod(os.environ["HOME"], 0o500)
        self.addCleanup(os.chmod, os.environ["HOME"], 0o700)
        self.assertEqual(paths.home(), os.path.join(os.environ["HOME"], ".local", "state", "passnote"))
        _, out, _ = self.run_cli(self.a, "doctor")
        self.assertIn('"allowWrite":["~/.local/state/passnote"]', out)

    def test_writable_by_others_home_fails_with_the_fix_and_exits_one(self):
        os.makedirs(self.home, mode=0o700)
        os.chmod(self.home, 0o770)
        code, out, err = self.run_cli(self.a, "doctor")
        self.assertEqual(code, 1)
        self.assertIn("chmod 700", out)
        self.assertEqual(err, "")

    def test_missing_home_is_created_and_reported_private(self):
        statuses = self.statuses(self.env(self.a))
        self.assertEqual([s for label, s in statuses.items() if label.startswith("storage ") and "private" in label], ["ok"])
        self.assertEqual(statuses["storage is writable from this shell"], "ok")

    def test_cli_doctor_exit_code(self):
        code, out, _ = self.run_cli(self.a, "doctor")
        self.assertIn(code, (0, 1))
        self.assertIn("python", out)
        self.assertIn("not on PATH", out)


if __name__ == "__main__":
    unittest.main()
