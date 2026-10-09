import json
import multiprocessing
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from support import HomeCase, new_sid
from passnote import paths, sessions


def _add_rooms(home, sid, prefix, count, start):
    os.environ["PASSNOTE_HOME"] = home
    start.wait(60)  # both writers start together, so their read-modify-writes really overlap
    for i in range(count):
        sessions.update_meta(sid, lambda meta, i=i: meta["rooms"].append(f"{prefix}{i}"))


def _reap(proc):
    if proc.is_alive():
        proc.kill()
        proc.join(5)


class SessionsTest(HomeCase):
    def setUp(self):
        super().setUp()
        paths.ensure_home()
        self.sid = new_sid()

    def test_meta_defaults_and_joined_marker(self):
        meta = sessions.load_meta(self.sid)
        self.assertEqual(meta["rooms"], [])
        self.assertIsNone(meta["permission_mode"])
        marker = os.path.join(paths.session_dir(self.sid), "joined")
        meta["rooms"] = ["r"]
        sessions.save_meta(self.sid, meta)
        self.assertTrue(os.path.exists(marker))
        meta["rooms"] = []
        sessions.save_meta(self.sid, meta)
        self.assertFalse(os.path.exists(marker))

    def test_active_age(self):
        self.assertIsNone(sessions.active_age(self.sid))
        now = time.time()
        sessions.touch_active(self.sid, now - 120)
        self.assertAlmostEqual(sessions.active_age(self.sid, now), 120, delta=1)

    def test_warmth_is_warm_within_the_ttl_else_cold(self):
        now = time.time()
        self.assertEqual(sessions.warmth(self.sid, now), ("cold", None, sessions.DEFAULT_TTL))
        sessions.touch_active(self.sid, now - 120)
        state, age, ttl = sessions.warmth(self.sid, now)
        self.assertEqual((state, ttl), ("warm", sessions.DEFAULT_TTL))
        self.assertAlmostEqual(age, 120, delta=1)
        sessions.update_meta(self.sid, lambda meta: meta.update(ttl_seconds=60))
        self.assertEqual(sessions.warmth(self.sid, now)[::2], ("cold", 60))

    def test_current_sid(self):
        with self.assertRaises(paths.PassnoteError):
            sessions.current_sid({})
        self.assertEqual(sessions.current_sid({"CLAUDE_CODE_SESSION_ID": self.sid}), self.sid)

    def test_by_pid_round_trip_and_start_time(self):
        started = sessions.pid_started_at(os.getpid())
        self.assertTrue(started)
        sessions.write_by_pid(os.getpid(), {"sid": self.sid, "pid_started_at": started})
        self.assertEqual(sessions.read_by_pid(os.getpid())["sid"], self.sid)
        self.assertIsNone(sessions.pid_started_at(999999999))

    def test_pid_started_at_is_tz_and_locale_independent(self):
        with mock.patch.dict(os.environ, {"TZ": "UTC", "LC_ALL": ""}):
            first = sessions.pid_started_at(os.getpid())
        with mock.patch.dict(os.environ, {"TZ": "Asia/Tokyo", "LC_ALL": ""}):
            second = sessions.pid_started_at(os.getpid())
        self.assertTrue(first)
        self.assertEqual(first, second)

    # -- P3: Linux reads the start time from /proc, no ps subprocess ---------

    def _fake_proc(self, stat_by_pid, mounted=True):
        root = os.path.join(self.tmp, "proc")
        os.makedirs(os.path.join(root, "sys", "kernel", "random"))
        with open(os.path.join(root, "sys", "kernel", "random", "boot_id"), "w") as fh:
            fh.write("6b4e0f6e-0000-4000-8000-000000000001\n")
        if mounted:
            stat_by_pid = dict(stat_by_pid, self="1 (python3) S" + " 0" * 50)
        for pid, stat in stat_by_pid.items():
            os.makedirs(os.path.join(root, str(pid)))
            with open(os.path.join(root, str(pid), "stat"), "w") as fh:
                fh.write(stat)
        for name, value in (("_LINUX", True), ("PROC", root)):
            patcher = mock.patch.object(sessions, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_linux_start_time_is_field_22_of_proc_stat(self):
        # comm (field 2) may contain spaces and ")": parse after the LAST ")".
        fields = ["S"] + [str(n) for n in range(4, 22)] + ["987654", "23", "24"]
        self._fake_proc({4242: "4242 (evil) name (x)) " + " ".join(fields) + "\n"})
        with mock.patch.object(sessions, "_ps_started_at", side_effect=AssertionError("no ps on Linux")):
            first = sessions.pid_started_at(4242)
            self.assertEqual(sessions.pid_started_at("4242"), first)
        self.assertEqual(first, "6b4e0f6e-0000-4000-8000-000000000001+987654")

    def test_linux_start_time_none_for_missing_or_garbled_stat(self):
        self._fake_proc({7: "7 (short) S 1 2 3\n", 8: "no parenthesis " + "1 " * 30, 9: "9 (x) S" + " a" * 30})
        with mock.patch.object(sessions, "_ps_started_at", side_effect=AssertionError("no ps on Linux")):
            for pid in (7, 8, 9, 10):
                with self.subTest(pid=pid):
                    self.assertIsNone(sessions.pid_started_at(pid))

    def test_linux_without_proc_falls_back_to_ps(self):
        self._fake_proc({}, mounted=False)
        with mock.patch.object(sessions, "_ps_started_at", return_value="from ps") as ps:
            self.assertEqual(sessions.pid_started_at(4242), "from ps")
        ps.assert_called_once_with(4242)

    def test_registry_name_only_when_user_set(self):
        reg = os.path.join(self.claude_home, "sessions")
        os.makedirs(reg)
        with open(os.path.join(reg, "4242.json"), "w") as fh:
            json.dump({"name": "session-a", "nameSource": "user"}, fh)
        with open(os.path.join(reg, "4343.json"), "w") as fh:
            json.dump({"name": "proj-3f", "nameSource": "derived"}, fh)
        self.assertEqual(sessions.registry_name(4242), "session-a")
        self.assertIsNone(sessions.registry_name(4343))
        self.assertIsNone(sessions.registry_name(1))

    def test_sanitize_title(self):
        self.assertEqual(sessions.sanitize_title("GitHub/Linear issues audit"), "GitHub-Linear-issues-audit")
        self.assertIsNone(sessions.sanitize_title("///"))
        self.assertIsNone(sessions.sanitize_title("User"))
        self.assertEqual(len(sessions.sanitize_title("x" * 200)), 64)

    def test_resolve_name_order(self):
        reg = os.path.join(self.claude_home, "sessions")
        os.makedirs(reg)
        with open(os.path.join(reg, "77.json"), "w") as fh:
            json.dump({"name": "from-registry", "nameSource": "user"}, fh)
        self.assertEqual(sessions.resolve_name("explicit", 77, "t"), ("explicit", "as"))
        self.assertEqual(sessions.resolve_name(None, 77, "t"), ("from-registry", "registry"))
        self.assertEqual(sessions.resolve_name(None, 78, "My Title"), ("My-Title", "title"))
        self.assertEqual(sessions.resolve_name(None, None, None), (None, None))
        with self.assertRaises(paths.PassnoteError):
            sessions.resolve_name("all", None, None)

    def test_emit_state(self):
        self.assertEqual(sessions.load_emit(self.sid), {"emitted": [], "overflow": [], "ahead": [], "receipts": []})
        ref = {"room": "r", "off": 0, "len": 10, "id": "a1"}
        sessions.save_emit(self.sid, [ref], [])
        self.assertEqual(sessions.load_emit(self.sid)["emitted"], [ref])
        sessions.save_emit(self.sid, [], [ref], [dict(ref, seq=1)])
        self.assertEqual(sessions.load_emit(self.sid),
                         {"emitted": [], "overflow": [ref], "ahead": [dict(ref, seq=1)], "receipts": []})
        receipt = {"room": "r", "id": "a1", "seq": 1, "ts": 1.0, "mode": "default", "to": ["bob"]}
        sessions.save_emit(self.sid, [], [], [], [receipt])
        self.assertEqual(sessions.load_emit(self.sid)["receipts"], [receipt])

    def test_recorded_mode(self):
        self.assertIsNone(sessions.recorded_mode(self.sid))
        meta = sessions.load_meta(self.sid)
        meta["permission_mode"] = "plan"
        sessions.save_meta(self.sid, meta)
        self.assertEqual(sessions.recorded_mode(self.sid), "plan")
        self.assertIsNone(sessions.recorded_mode("not-a-sid"))

    # -- C1: meta read-modify-write under a per-session meta lock -----------

    def test_update_meta_applies_and_saves(self):
        result = sessions.update_meta(self.sid, lambda meta: meta.update(permission_mode="plan"))
        self.assertEqual(result["permission_mode"], "plan")
        self.assertEqual(sessions.load_meta(self.sid)["permission_mode"], "plan")

    def test_update_meta_skips_the_save_when_fn_returns_false(self):
        sessions.update_meta(self.sid, lambda meta: False)
        self.assertFalse(os.path.exists(sessions.meta_path(self.sid)))

    def test_update_meta_loses_no_concurrent_update(self):
        ctx = multiprocessing.get_context("spawn")
        start = ctx.Event()
        count = 150
        procs = [ctx.Process(target=_add_rooms, args=(self.home, self.sid, prefix, count, start))
                 for prefix in ("p", "q")]
        for proc in procs:
            proc.start()
            self.addCleanup(_reap, proc)
        start.set()
        for proc in procs:
            proc.join(120)
            self.assertEqual(proc.exitcode, 0)
        rooms = sessions.load_meta(self.sid)["rooms"]
        self.assertEqual(sorted(rooms), sorted([f"p{i}" for i in range(count)] + [f"q{i}" for i in range(count)]))

    def test_meta_lock_is_not_the_delivery_lock(self):
        # The hook holds the delivery lock (non-blocking) around read -> emit -> advance and may
        # update meta inside it; the meta lock must be a different file.
        with sessions.session_lock(self.sid):
            sessions.update_meta(self.sid, lambda meta: meta.update(title="t"))
        self.assertEqual(sessions.load_meta(self.sid)["title"], "t")

    def test_update_meta_times_out_when_the_meta_lock_is_held(self):
        with mock.patch.object(sessions, "META_LOCK_TIMEOUT", 0.05):
            with sessions.meta_lock(self.sid):
                with self.assertRaises(paths.LockBusy):
                    sessions.update_meta(self.sid, lambda meta: None)

    def test_session_lock_is_nonblocking(self):
        with sessions.session_lock(self.sid):
            with self.assertRaises(paths.LockBusy):
                with sessions.session_lock(self.sid):
                    pass

    # --- ruling 2: write_by_pid mirrors pid/pid_started_at into session meta ---

    def test_write_by_pid_mirrors_pid_into_meta_without_touching_rooms(self):
        meta = sessions.load_meta(self.sid)
        meta["rooms"] = ["r"]
        sessions.save_meta(self.sid, meta)

        started = sessions.pid_started_at(os.getpid())
        sessions.write_by_pid(os.getpid(), {"sid": self.sid, "pid_started_at": started})

        meta2 = sessions.load_meta(self.sid)
        self.assertEqual(meta2["pid"], os.getpid())
        self.assertEqual(meta2["pid_started_at"], started)
        self.assertEqual(meta2["rooms"], ["r"])

    def test_record_pid_writes_record_and_mirrors_meta(self):
        sessions.record_pid(os.getpid(), self.sid)
        token = sessions.pid_started_at(os.getpid())
        self.assertEqual(sessions.read_by_pid(os.getpid()), {"sid": self.sid, "pid_started_at": token})
        sessions.record_pid(os.getpid(), self.sid, prev_sid="prev")
        self.assertEqual(sessions.read_by_pid(os.getpid())["prev_sid"], "prev")
        meta = sessions.load_meta(self.sid)
        self.assertEqual((meta["pid"], meta["pid_started_at"]), (os.getpid(), token))

    def test_record_pid_ignores_an_invalid_pid(self):
        sessions.record_pid("garbage", self.sid)
        self.assertIsNone(sessions.load_meta(self.sid)["pid"])

    def test_write_by_pid_skips_meta_mirror_when_sid_invalid_or_missing(self):
        # Should not raise, and should not create any session dir for a bogus/missing sid.
        sessions.write_by_pid(555555, {"sid": "not-a-sid"})
        sessions.write_by_pid(555556, {})
        self.assertIsNone(sessions.read_by_pid(555555).get("pid_started_at"))

    # --- ruling 3: pid_alive / process_state ---

    def test_process_state_unknown_without_pid(self):
        self.assertEqual(sessions.process_state(self.sid), "unknown")

    def test_process_state_running_for_self_with_real_start_time(self):
        meta = sessions.load_meta(self.sid)
        meta["pid"] = os.getpid()
        meta["pid_started_at"] = sessions.pid_started_at(os.getpid())
        sessions.save_meta(self.sid, meta)
        self.assertEqual(sessions.process_state(self.sid), "running")

    def test_process_state_gone_for_dead_pid(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        dead_pid = proc.pid

        meta = sessions.load_meta(self.sid)
        meta["pid"] = dead_pid
        meta["pid_started_at"] = "whatever start time"
        sessions.save_meta(self.sid, meta)
        self.assertEqual(sessions.process_state(self.sid), "gone")

    def test_process_state_gone_for_mismatched_recorded_start_time(self):
        meta = sessions.load_meta(self.sid)
        meta["pid"] = os.getpid()
        meta["pid_started_at"] = "definitely-not-the-real-start-time"
        sessions.save_meta(self.sid, meta)
        self.assertEqual(sessions.process_state(self.sid), "gone")

    def test_process_state_registry_fallback_both_ways(self):
        reg = os.path.join(self.claude_home, "sessions")
        os.makedirs(reg, exist_ok=True)
        pid = os.getpid()

        meta = sessions.load_meta(self.sid)
        meta["pid"] = pid
        meta["pid_started_at"] = None
        sessions.save_meta(self.sid, meta)

        registry_file = os.path.join(reg, f"{pid}.json")
        with open(registry_file, "w") as fh:
            json.dump({}, fh)
        self.assertEqual(sessions.process_state(self.sid), "running")

        os.unlink(registry_file)
        self.assertEqual(sessions.process_state(self.sid), "gone")

    def test_pid_alive_rejects_non_positive_and_non_int(self):
        # os.kill(0, 0) signals our own process group and os.kill(-1, 0) every process: both "alive".
        for bad in (0, -1, True, False, None, "", "0", "abc", "-1", " 1", "\u00b2", "\u0663", "1" * 5000,
                    str(2 ** 31), 1.5, 2 ** 70, [1]):
            with self.subTest(pid=bad):
                self.assertIsNone(sessions.parse_pid(bad))
                self.assertFalse(sessions.pid_alive(bad))
                self.assertIsNone(sessions.pid_started_at(bad))
                self.assertIsNone(sessions.read_by_pid(bad))
                sessions.write_by_pid(bad, {"sid": self.sid})  # ignored, never raises
        self.assertFalse(os.path.exists(os.path.join(self.home, "sessions", "by-pid")))
        self.assertFalse(os.path.exists(paths.session_dir(self.sid)))

    def test_pid_from_the_environment_is_a_digit_string(self):
        # CLAUDE_PID reaches us as a string; ASCII digits are a valid pid.
        me = str(os.getpid())
        self.assertEqual(sessions.parse_pid(me), os.getpid())
        self.assertTrue(sessions.pid_alive(me))
        self.assertEqual(sessions.pid_started_at(me), sessions.pid_started_at(os.getpid()))

    def test_process_state_unknown_for_corrupt_pid(self):
        for bad in (-1, 0, True, "abc", [1], 2 ** 70):
            with self.subTest(pid=bad):
                meta = sessions.load_meta(self.sid)
                meta["pid"] = bad
                meta["pid_started_at"] = "x"
                sessions.save_meta(self.sid, meta)
                self.assertEqual(sessions.process_state(self.sid), "unknown")

    def test_load_meta_type_checks_rooms(self):
        for stored, expected in (("abc", []), ({"r": 1}, []), (["r", 5, "../x", None, "s"], ["r", "s"])):
            with self.subTest(rooms=stored):
                paths.atomic_write_json(sessions.meta_path(self.sid), {"rooms": stored})
                self.assertEqual(sessions.load_meta(self.sid)["rooms"], expected)

    def test_pid_alive(self):
        self.assertTrue(sessions.pid_alive(os.getpid()))
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertFalse(sessions.pid_alive(proc.pid))


if __name__ == "__main__":
    unittest.main()
