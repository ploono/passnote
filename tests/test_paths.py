import json
import os
import random
import stat
import unittest
import uuid

from support import HomeCase, new_sid
from passnote import paths


class PathsTest(HomeCase):
    def test_home_defaults_to_xdg_state(self):
        del os.environ["PASSNOTE_HOME"]
        os.environ["XDG_STATE_HOME"] = os.path.join(self.tmp, "state")
        self.assertEqual(paths.home(), os.path.join(self.tmp, "state", "passnote"))

    def test_relative_home_values_are_ignored(self):
        # Hooks run in different cwds; a relative root would give each project its own storage.
        os.environ["HOME"] = os.path.join(self.tmp, "user")
        default = os.path.join(self.tmp, "user", ".local", "state", "passnote")
        os.environ["PASSNOTE_HOME"] = "relative/passnote"
        self.assertEqual(paths.home(), default)
        del os.environ["PASSNOTE_HOME"]
        os.environ["XDG_STATE_HOME"] = "relative-state"
        self.assertEqual(paths.home(), default)
        os.environ["PASSNOTE_HOME"] = "~/pn"
        self.assertEqual(paths.home(), os.path.join(self.tmp, "user", "pn"))

    def test_ensure_home_creates_private_dir(self):
        root = paths.ensure_home()
        self.assertEqual(stat.S_IMODE(os.stat(root).st_mode), 0o700)

    def test_ensure_home_rejects_group_writable(self):
        os.makedirs(self.home)
        os.chmod(self.home, 0o770)
        with self.assertRaises(paths.PassnoteError) as ctx:
            paths.ensure_home()
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("chmod 700", str(ctx.exception))

    def test_names(self):
        for good in ("session-a", "A.b_c-1", "x" * 64):
            with self.subTest(good=good):
                self.assertEqual(paths.check_name(good), good)
        for bad in ("", ".", "..", "a/b", "a b", "x" * 65, "../etc", "worker:1", "r\n"):
            with self.subTest(bad=bad), self.assertRaises(paths.PassnoteError):
                paths.check_name(bad)
        for reserved in ("all", "User", "HUMAN", "system", "claude", "assistant"):
            with self.subTest(reserved=reserved), self.assertRaises(paths.PassnoteError):
                paths.check_name(reserved)
        self.assertEqual(paths.check_name("all", member=False), "all")

    def test_check_sid(self):
        sid = new_sid()
        self.assertEqual(paths.check_sid(sid), sid)
        self.assertEqual(paths.check_sid(sid.upper()), sid)
        for bad in ("../x", "", None, "1234"):
            with self.subTest(bad=bad), self.assertRaises(paths.PassnoteError):
                paths.check_sid(bad)

    def test_check_sid_accepts_only_the_canonical_form(self):
        sid = new_sid()
        compact = sid.replace("-", "")
        for bad in ("{" + sid + "}", "urn:uuid:" + sid, compact, sid + "\n", " " + sid, sid[:-1],
                    sid + "0", sid.replace("-", "_"), sid[:7] + "-" + sid[7] + sid[9:], "g" + sid[1:],
                    "\uff10" + sid[1:], b"x", 123, [sid]):
            with self.subTest(bad=bad), self.assertRaises(paths.PassnoteError):
                paths.check_sid(bad)
        self.assertEqual(paths.check_sid(uuid.UUID(sid)), sid)  # str() of a UUID is canonical

    def test_check_sid_matches_the_uuid_module_reference(self):
        # The previous implementation, kept here as the reference: same accepted set, same result.
        def reference(value):
            try:
                parsed = uuid.UUID(str(value))
            except (ValueError, TypeError):
                return None
            if value is None or str(parsed) != str(value).lower():
                return None
            return str(parsed)

        rng = random.Random(5)
        alphabet = "0123456789abcdefABCDEF-{}:urnid gG_\u0660\uff21"
        candidates = [new_sid() for _ in range(50)]
        candidates += [s.upper() for s in candidates[:10]]
        for _ in range(3000):
            base = list(rng.choice(candidates))
            for _ in range(rng.randint(1, 3)):
                op = rng.randrange(3)
                pos = rng.randrange(len(base) + 1)
                if op == 0 and base:
                    base[min(pos, len(base) - 1)] = rng.choice(alphabet)
                elif op == 1:
                    base.insert(pos, rng.choice(alphabet))
                elif base:
                    del base[min(pos, len(base) - 1)]
            candidates.append("".join(base))
        for value in candidates + [None, 0, b"", "urn:uuid:" + candidates[0], "{" + candidates[0] + "}"]:
            with self.subTest(value=value):
                expected = reference(value)
                if expected is None:
                    with self.assertRaises(paths.PassnoteError):
                        paths.check_sid(value)
                else:
                    self.assertEqual(paths.check_sid(value), expected)

    def test_valid_sid(self):
        self.assertTrue(paths.valid_sid("0f1e2d3c-4b5a-4968-8776-655443322110"))
        for bad in ("../x", "", None, 5, "S-A"):
            self.assertFalse(paths.valid_sid(bad), bad)

    def test_atomic_write_text_writes_0600_and_leaves_no_temp_file(self):
        path = os.path.join(paths.ensure_home(), "rooms", "r", "files", "a1.txt")
        paths.atomic_write_text(path, "漢\nline two")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "漢\nline two")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.listdir(os.path.dirname(path)), ["a1.txt"])

    def test_atomic_write_json_is_private(self):
        paths.ensure_home()
        target = os.path.join(self.home, "sub", "x.json")
        paths.atomic_write_json(target, {"a": 1})
        self.assertEqual(paths.read_json(target), {"a": 1})
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.assertEqual(paths.read_json(os.path.join(self.home, "missing.json"), {}), {})

    def test_atomic_write_json_leaves_no_temp_file(self):
        paths.ensure_home()
        target = os.path.join(self.home, "sub", "x.json")
        paths.atomic_write_json(target, {"a": 1})
        paths.atomic_write_json(target, {"a": 2})
        with self.assertRaises(TypeError):
            paths.atomic_write_json(target, {"a": object()})  # not serializable: nothing replaced
        self.assertEqual(os.listdir(os.path.dirname(target)), ["x.json"])
        self.assertEqual(paths.read_json(target), {"a": 2})

    def test_read_json_tolerates_corruption(self):
        paths.ensure_home()
        target = os.path.join(self.home, "bad.json")
        with open(target, "w") as fh:
            fh.write("{not json")
        self.assertEqual(paths.read_json(target, "d"), "d")
        # Room config/members/meta files are writable by every member: deep nesting is corruption too.
        with open(target, "w") as fh:
            fh.write("[" * 200000 + "]" * 200000)
        self.assertEqual(paths.read_json(target, "d"), "d")

    def test_file_lock_nonblocking_reports_busy(self):
        paths.ensure_home()
        lock_path = os.path.join(self.home, ".lock")
        with paths.FileLock(lock_path):
            with self.assertRaises(paths.LockBusy):
                with paths.FileLock(lock_path, blocking=False):
                    pass
            with self.assertRaises(paths.LockBusy):
                with paths.FileLock(lock_path, timeout=0.1):
                    pass
        with paths.FileLock(lock_path, blocking=False):
            pass

    def test_log_error_caps_size(self):
        paths.ensure_home()
        log = os.path.join(self.home, "errors.log")
        with open(log, "w") as fh:
            fh.write("x" * (paths.ERROR_LOG_MAX + 1))
        paths.log_error("test", ValueError("boom"))
        self.assertTrue(os.path.exists(log + ".1"))
        with open(log) as fh:
            self.assertEqual(json.loads(fh.read())["error"], "ValueError")

    def test_log_error_never_records_the_exception_message(self):
        # str(exc) can echo caller data: int() errors repeat their input, PassnoteError names.
        paths.ensure_home()
        paths.log_error("t1", ValueError("invalid literal: secret-text"))
        paths.log_error("t2", FileNotFoundError(2, "No such file or directory", "/srv/secret-name"))
        with open(os.path.join(self.home, "errors.log")) as fh:
            raw = fh.read()
        self.assertNotIn("secret", raw)
        first, second = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual((first["where"], first["error"]), ("t1", "ValueError"))
        self.assertEqual(second["error"], "FileNotFoundError")
        self.assertEqual((second["errno"], second["strerror"]), (2, "No such file or directory"))

    def test_log_error_records_a_fixed_note(self):
        paths.ensure_home()
        paths.log_error("hook", note="transcript unreadable")
        with open(os.path.join(self.home, "errors.log")) as fh:
            record = json.loads(fh.read())
        self.assertEqual((record["where"], record["note"]), ("hook", "transcript unreadable"))
        self.assertNotIn("error", record)


if __name__ == "__main__":
    unittest.main()
