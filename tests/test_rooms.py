import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

from support import HomeCase, new_sid
from passnote import cursor, paths, render, rooms, sessions, store


class RoomsTest(HomeCase):
    def setUp(self):
        super().setUp()
        paths.ensure_home()
        self.a, self.b = new_sid(), new_sid()

    def test_default_room_uses_main_worktree_root(self):
        repo = os.path.join(self.tmp, "my repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q", repo], check=True)
        sub = os.path.join(repo, "src")
        os.makedirs(sub)
        room, display, root = rooms.default_room(sub)
        self.assertEqual(root, os.path.realpath(repo))
        self.assertEqual(display, "my-repo")
        self.assertRegex(room, r"^my-repo-[0-9a-f]{4}$")
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.assertEqual(rooms.default_room(plain)[2], os.path.realpath(plain))

    def test_default_room_digest_is_sha1_not_used_for_security(self):
        # usedforsecurity=False keeps FIPS builds working; the digest itself must not change,
        # because that would rename everyone's rooms.
        import hashlib
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        with mock.patch("hashlib.sha1", wraps=hashlib.sha1) as sha1:
            room, _, root = rooms.default_room(plain)
        self.assertEqual(sha1.call_args.kwargs, {"usedforsecurity": False})
        self.assertEqual(room, "plain-" + hashlib.sha1(root.encode("utf-8")).hexdigest()[:4])

    def test_join_creates_cursor_at_eof_and_membership(self):
        store.append_message("r", {"text": "before"}, "z")
        res = rooms.join(self.a, "r", "alice", "/root", display="r")
        self.assertEqual(res["members"], ["alice"])
        self.assertEqual(res["alias"], "a")
        self.assertEqual(cursor.load(self.a, "r")["seq"], 1)
        self.assertEqual(sessions.load_meta(self.a)["rooms"], ["r"])
        self.assertEqual(store.load_meta("r")["root"], "/root")
        self.assertEqual(store.read_events("r")[-1]["type"], "join")

    def test_aliases_are_unique_and_skip_w(self):
        rooms.join(self.a, "r", "alice", "/root")
        res = rooms.join(self.b, "r", "anna", "/root")
        self.assertEqual(res["alias"], "an")
        c = new_sid()
        self.assertEqual(rooms.join(c, "r", "walt", "/root")["alias"], "wa")

    def test_join_from_other_root_warns(self):
        rooms.join(self.a, "r", "alice", "/root")
        res = rooms.join(self.b, "r", "bob", "/elsewhere")
        self.assertIn("/root", res["warning"])

    # -- Takeover only when gone (ticket 01): clash cases --------------------

    def test_name_clash_running_cold_is_error(self):
        """A running-but-cold member: pid alive, start time matches, but inactive for hours."""
        rooms.join(self.a, "r", "alice", "/root")
        sessions.write_by_pid(os.getpid(), {
            "sid": self.a,
            "pid_started_at": sessions.pid_started_at(os.getpid()),
        })
        sessions.touch_active(self.a, time.time() - 10 * 3600)
        with self.assertRaises(paths.PassnoteError):
            rooms.join(self.b, "r", "alice", "/root")

    def test_name_clash_gone_dead_pid_is_takeover(self):
        """A gone member via a dead pid: takeover, newcomer inherits the cursor."""
        rooms.join(self.a, "r", "alice", "/root")
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        pid = proc.pid
        proc.wait()
        sessions.write_by_pid(pid, {"sid": self.a, "pid_started_at": None})
        store.append_message("r", {"text": "later"}, "z")
        inherited = cursor.load(self.a, "r")
        rooms.join(self.b, "r", "alice", "/root")
        self.assertNotIn(self.a, store.load_members("r"))
        self.assertEqual(cursor.load(self.b, "r"), inherited)
        self.assertEqual(sessions.load_meta(self.a)["rooms"], [])

    def test_takeover_interrupted_by_lock_busy_keeps_the_inherited_cursor(self):
        rooms.join(self.a, "r", "alice", "/root")
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        sessions.write_by_pid(proc.pid, {"sid": self.a, "pid_started_at": None})
        store.append_message("r", {"text": "later"}, "z")
        inherited = cursor.load(self.a, "r")
        with mock.patch.object(sessions, "update_meta", side_effect=paths.LockBusy("meta")):
            with self.assertRaises(paths.LockBusy):
                rooms.join(self.b, "r", "alice", "/root")
        self.assertEqual(cursor.load(self.a, "r"), inherited)
        self.assertIn(self.a, store.load_members("r"))
        rooms.join(self.b, "r", "alice", "/root")  # the retry still inherits
        self.assertEqual(cursor.load(self.b, "r"), inherited)
        self.assertIsNone(cursor.load(self.a, "r"))
        self.assertNotIn(self.a, store.load_members("r"))

    def test_name_clash_gone_start_mismatch_is_takeover(self):
        """A gone member via a pid_started_at mismatch (pid reuse guard): takeover."""
        rooms.join(self.a, "r", "alice", "/root")
        sessions.write_by_pid(os.getpid(), {
            "sid": self.a,
            "pid_started_at": "Thu Jan  1 00:00:00 1970",
        })
        rooms.join(self.b, "r", "alice", "/root")
        self.assertNotIn(self.a, store.load_members("r"))
        self.assertEqual(sessions.load_meta(self.a)["rooms"], [])

    def test_name_clash_unknown_is_error(self):
        """No pid info at all: unknown counts as running, so the clash is an error."""
        rooms.join(self.a, "r", "alice", "/root")
        with self.assertRaises(paths.PassnoteError):
            rooms.join(self.b, "r", "alice", "/root")

    def test_leave_removes_member_and_cursor(self):
        rooms.join(self.a, "r", "alice", "/root")
        self.assertTrue(rooms.leave(self.a, "r"))
        self.assertEqual(store.load_members("r"), {})
        self.assertIsNone(cursor.load(self.a, "r"))
        self.assertEqual(sessions.load_meta(self.a)["rooms"], [])
        self.assertFalse(rooms.leave(self.a, "r"))

    def test_rename_refuses_running_clash(self):
        rooms.join(self.a, "r", "alice", "/root")
        rooms.join(self.b, "r", "bob", "/root")
        sessions.write_by_pid(os.getpid(), {
            "sid": self.b,
            "pid_started_at": sessions.pid_started_at(os.getpid()),
        })
        self.assertFalse(rooms.rename(self.a, ["r"], "bob"))
        self.assertTrue(rooms.rename(self.a, ["r"], "alicia"))
        self.assertEqual(store.load_members("r")[self.a]["name"], "alicia")

    def test_carry_over_moves_membership_and_cursor(self):
        rooms.join(self.a, "r", "alice", "/root")
        meta = sessions.load_meta(self.a)
        meta["permission_mode"] = "default"
        sessions.save_meta(self.a, meta)
        cur = cursor.load(self.a, "r")
        new = new_sid()
        rooms.carry_over(self.a, new)
        self.assertIn(new, store.load_members("r"))
        self.assertNotIn(self.a, store.load_members("r"))
        self.assertEqual(cursor.load(new, "r"), cur)
        self.assertEqual(sessions.load_meta(new)["rooms"], ["r"])
        self.assertEqual(sessions.load_meta(new)["permission_mode"], "default")
        self.assertFalse(os.path.exists(paths.session_dir(self.a)))

    def test_carry_over_remembers_the_last_16_earlier_session_ids(self):
        rooms.join(self.a, "r", "alice", "/root")
        sids = [self.a] + [new_sid() for _ in range(18)]
        for old, new in zip(sids, sids[1:]):
            rooms.carry_over(old, new)
        members = store.load_members("r")
        self.assertEqual(list(members), [sids[-1]])
        self.assertEqual(members[sids[-1]]["prev_sids"], sids[-17:-1])
        self.assertEqual(store.member_for_sid(members, sids[-2])[0], sids[-1])
        self.assertIsNone(store.member_for_sid(members, self.a))  # past the bound

    def test_rejoin_after_clear_keeps_the_earlier_session_ids(self):
        rooms.join(self.a, "r", "alice", "/root")
        new = new_sid()
        rooms.carry_over(self.a, new)
        rooms.join(new, "r", "alice", "/root")
        self.assertEqual(store.load_members("r")[new]["prev_sids"], [self.a])

    def test_rejoin_keeps_prefs_and_takeover_does_not(self):
        rooms.join(self.b, "r", "bob", "/root")
        entry = rooms.set_prefs(self.b, "r", threads=["auth"], digest=True)
        self.assertEqual((entry["threads"], entry["digest"]), (["auth"], True))
        rooms.join(self.b, "r", "bob", "/root")
        kept = store.load_members("r")[self.b]
        self.assertEqual((kept["threads"], kept["digest"]), (["auth"], True))
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        sessions.write_by_pid(proc.pid, {"sid": self.b, "pid_started_at": None})
        newcomer = new_sid()
        rooms.join(newcomer, "r", "bob", "/root")
        self.assertNotIn(self.b, store.load_members("r"))
        entry = store.load_members("r")[newcomer]
        self.assertNotIn("threads", entry)
        self.assertNotIn("digest", entry)

    def test_set_prefs_keeps_removes_and_needs_membership(self):
        rooms.join(self.b, "r", "bob", "/root")
        rooms.set_prefs(self.b, "r", threads=["auth"], digest=True)
        rooms.set_prefs(self.b, "r", threads=[])  # [] is a filter (only unthreaded and addressed lines)
        self.assertEqual(store.load_members("r")[self.b]["threads"], [])
        self.assertTrue(store.load_members("r")[self.b]["digest"])
        rooms.set_prefs(self.b, "r", threads=None, digest=False)
        self.assertNotIn("threads", store.load_members("r")[self.b])
        self.assertNotIn("digest", store.load_members("r")[self.b])
        with self.assertRaises(paths.PassnoteError) as ctx:
            rooms.set_prefs(self.a, "r", threads=["auth"])
        self.assertEqual(ctx.exception.code, 3)

    def test_subscription_survives_clear_and_rejoin(self):
        rooms.join(self.b, "r", "bob", "/root")
        rooms.set_prefs(self.b, "r", threads=["auth"])
        new = new_sid()
        rooms.carry_over(self.b, new)
        self.assertEqual(store.load_members("r")[new]["threads"], ["auth"])
        rooms.join(new, "r", "bob", "/root")  # join rebuilds the entry: it must keep the setting
        self.assertEqual(store.load_members("r")[new]["threads"], ["auth"])

    def test_carry_over_creates_the_new_session_dir_before_moving_membership(self):
        rooms.join(self.a, "r", "alice", "/root")
        new = new_sid()
        seen = []
        real = store.save_members

        def spy(room, members):
            seen.append((new in members, rooms.is_running(new)))
            return real(room, members)

        with mock.patch.object(store, "save_members", spy):
            rooms.carry_over(self.a, new)
        self.assertEqual(seen, [(True, True)])

    def test_carry_over_pid_fields_fall_back_to_the_new_session(self):
        rooms.join(self.a, "r", "alice", "/root")
        new = new_sid()
        sessions.update_meta(new, lambda meta: meta.update(pid=os.getpid(), pid_started_at="tok"))
        rooms.carry_over(self.a, new)
        meta = sessions.load_meta(new)
        self.assertEqual((meta["pid"], meta["pid_started_at"]), (os.getpid(), "tok"))

    def test_carry_over_carries_only_unconfirmed_emissions_checked_in_the_old_transcript(self):
        rooms.join(self.a, "r", "alice", "/root")
        path = os.path.join(self.tmp, "old.jsonl")
        record = {"type": "attachment", "attachment": {"type": "hook_additional_context",
                                                       "content": [render.HEADER + "\na1 shown"]}}
        with open(path, "w") as fh:
            fh.write(json.dumps(record) + "\n")
        sessions.update_meta(self.a, lambda meta: meta.update(transcript_path=path))
        ref = {"room": "r", "off": 0, "len": 10}
        sessions.save_emit(self.a, [dict(ref, id="a1"), dict(ref, id="a2")], [])
        new = new_sid()
        rooms.carry_over(self.a, new)
        state = sessions.load_emit(new)
        self.assertEqual(state["emitted"], [])
        self.assertEqual([r["id"] for r in state["overflow"]], ["a2"])

    def test_carry_over_confirms_an_emitted_line_only_in_its_own_room(self):
        rooms.join(self.a, "r", "alice", "/root")
        path = os.path.join(self.tmp, "old.jsonl")
        record = {"type": "attachment", "attachment": {"type": "hook_additional_context",
                                                       "content": [render.HEADER + "\na1 bob→all say: in r"]}}
        with open(path, "w") as fh:
            fh.write(json.dumps(record) + "\n")
        sessions.update_meta(self.a, lambda meta: meta.update(transcript_path=path))
        shown = {"room": "r", "off": 0, "len": 10, "id": "a1", "line": "a1 bob→all say: in r"}
        dropped = {"room": "s", "off": 0, "len": 10, "id": "a1", "line": "a1 bob→all say: in s"}
        sessions.save_emit(self.a, [shown, dropped], [])
        new = new_sid()
        rooms.carry_over(self.a, new)
        self.assertEqual(sessions.load_emit(new)["overflow"], [{"room": "s", "off": 0, "len": 10, "id": "a1"}])

    def test_carry_over_twice_is_harmless_and_never_blanks_the_new_rooms(self):
        rooms.join(self.a, "r", "alice", "/root")
        new = new_sid()
        rooms.carry_over(self.a, new)
        rooms.carry_over(self.a, new)
        self.assertEqual(sessions.load_meta(new)["rooms"], ["r"])

    def test_carry_over_moves_the_emit_state(self):
        rooms.join(self.a, "r", "alice", "/root")
        ref = {"room": "r", "off": 0, "len": 10, "id": "a1"}
        sessions.save_emit(self.a, [ref], [dict(ref, id="a2")], [dict(ref, id="a3", seq=3)])
        new = new_sid()
        rooms.carry_over(self.a, new)
        state = sessions.load_emit(new)
        # a1 was emitted and the old transcript can't confirm it: it is carried to render again
        self.assertEqual(state["emitted"], [])
        self.assertEqual([r["id"] for r in state["overflow"]], ["a2", "a1"])
        self.assertEqual([r["id"] for r in state["ahead"]], ["a3"])
        self.assertFalse(os.path.exists(paths.session_dir(self.a)))

    # -- gc (ticket 01): never prune a running session, however old ---------

    def test_gc_prunes_stale_sessions(self):
        rooms.join(self.a, "r", "alice", "/root")
        sessions.touch_active(self.a, time.time() - 30 * 86400)
        rooms.join(self.b, "r", "bob", "/root")
        sessions.touch_active(self.b)
        result = rooms.gc(max_age_days=7)
        self.assertEqual(result["sessions"], 1)
        self.assertEqual(list(store.load_members("r")), [self.b])

    def _make_gone(self, sid):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        sessions.write_by_pid(proc.pid, {"sid": sid, "pid_started_at": None})
        self.assertEqual(sessions.process_state(sid), "gone")

    def test_gc_keeps_a_gone_session_active_recently(self):
        """A closed session can be resumed with the same id (spec §5): gone is not enough to
        prune it, only gone (or unknown) and idle past max_age_days (decision 2026-10-03)."""
        rooms.join(self.a, "r", "alice", "/root")
        self._make_gone(self.a)
        sessions.touch_active(self.a)
        self.assertEqual(rooms.gc(max_age_days=7), {"sessions": 0, "members": 0, "pids": 1})
        self.assertEqual(list(store.load_members("r")), [self.a])
        self.assertIsNotNone(cursor.load(self.a, "r"))
        self.assertTrue(os.path.isdir(paths.session_dir(self.a)))
        self.assertEqual(sessions.load_meta(self.a)["rooms"], ["r"])

    def test_gc_prunes_a_gone_session_idle_past_the_limit(self):
        rooms.join(self.a, "r", "alice", "/root")
        self._make_gone(self.a)
        sessions.touch_active(self.a, time.time() - 8 * 86400)
        result = rooms.gc(max_age_days=7)
        self.assertEqual((result["sessions"], result["members"]), (1, 1))
        self.assertEqual(store.load_members("r"), {})
        self.assertFalse(os.path.exists(paths.session_dir(self.a)))

    def test_gc_ages_a_gone_never_active_session_by_its_dir(self):
        # Never active: no "active" file, so the session dir's mtime is its age.
        rooms.join(self.a, "r", "alice", "/root")
        self._make_gone(self.a)
        self.assertIsNone(sessions.active_age(self.a))
        self.assertEqual(rooms.gc(max_age_days=7)["sessions"], 0)  # a new dir: kept
        self.assertEqual(list(store.load_members("r")), [self.a])
        old = time.time() - 8 * 86400
        os.utime(paths.session_dir(self.a), (old, old))
        result = rooms.gc(max_age_days=7)
        self.assertEqual((result["sessions"], result["members"]), (1, 1))
        self.assertEqual(store.load_members("r"), {})
        self.assertFalse(os.path.exists(paths.session_dir(self.a)))

    def test_gc_keeps_an_unknown_session_active_recently(self):
        rooms.join(self.a, "r", "alice", "/root")
        sessions.touch_active(self.a)
        self.assertEqual(sessions.process_state(self.a), "unknown")
        self.assertEqual(rooms.gc(max_age_days=7)["sessions"], 0)
        self.assertEqual(list(store.load_members("r")), [self.a])

    def test_a_gone_member_name_is_still_taken_over_at_once(self):
        """Takeover (ticket 01) is unchanged: gc keeping a gone member doesn't block its name."""
        rooms.join(self.a, "r", "alice", "/root")
        self._make_gone(self.a)
        sessions.touch_active(self.a)
        rooms.join(self.b, "r", "alice", "/root")
        self.assertEqual(list(store.load_members("r")), [self.b])

    def test_gc_keeps_running_inactive_30_days(self):
        rooms.join(self.a, "r", "alice", "/root")
        sessions.write_by_pid(os.getpid(), {
            "sid": self.a,
            "pid_started_at": sessions.pid_started_at(os.getpid()),
        })
        sessions.touch_active(self.a, time.time() - 30 * 86400)
        result = rooms.gc(max_age_days=7)
        self.assertEqual(result["sessions"], 0)
        self.assertEqual(list(store.load_members("r")), [self.a])

    # -- Orphan members can't block a name forever (fix round 1) -------------

    def test_join_takes_over_orphan_member_with_no_session_dir(self):
        """A member whose session dir is gone (e.g. a crash between save_members and save_meta)
        is treated as gone, not "unknown": takeover, not an error."""
        rooms.join(self.a, "r", "alice", "/root")
        shutil.rmtree(paths.session_dir(self.a))
        res = rooms.join(self.b, "r", "alice", "/root")
        self.assertEqual(res["members"], ["alice"])
        self.assertNotIn(self.a, store.load_members("r"))
        self.assertIn(self.b, store.load_members("r"))
        # Dropping the room from the orphan's meta must not recreate its session dir.
        self.assertFalse(os.path.exists(paths.session_dir(self.a)))

    def test_join_with_non_uuid_member_key_is_takeover_not_error(self):
        """A members.json key that isn't a valid session id must not make join raise "invalid
        session id" -- it's treated as gone, same as any other orphan."""
        store.save_meta("r", {"root": "/root", "display": "r", "created_at": 0.0, "aliases_used": ["a"]})
        store.save_members("r", {"not-a-uuid": {"name": "alice", "alias": "a", "joined_at": 0.0, "root": "/root"}})
        res = rooms.join(self.a, "r", "alice", "/root")
        self.assertEqual(res["members"], ["alice"])
        self.assertNotIn("not-a-uuid", store.load_members("r"))
        self.assertIn(self.a, store.load_members("r"))

    def test_gc_sweeps_orphan_member_after_session_dir_pruned(self):
        """Reviewer repro: a crash between save_members and save_meta leaves a member whose
        session dir gc later prunes as stale (never listed in that session's meta["rooms"]). The
        sweep pass must reclaim the leftover member entry too, so a name isn't blocked forever and
        a second gc is a no-op."""
        rooms.join(self.a, "r", "alice", "/root")
        meta = sessions.load_meta(self.a)
        meta["rooms"] = []  # simulate the crash: save_members ran, save_meta never recorded "r"
        sessions.save_meta(self.a, meta)
        sessions.touch_active(self.a, time.time() - 30 * 86400)
        result = rooms.gc(max_age_days=7)
        self.assertEqual(result["sessions"], 1)
        self.assertEqual(result["members"], 1)
        self.assertEqual(store.load_members("r"), {})
        self.assertEqual(rooms.gc(max_age_days=7), {"sessions": 0, "members": 0, "pids": 0})
        rooms.join(self.b, "r", "alice", "/root")  # must not raise "in use by a running session"

    def test_orphan_sweep_leave_event_names_the_member(self):
        rooms.join(self.a, "r", "alice", "/root")
        shutil.rmtree(paths.session_dir(self.a))  # an orphan: no session dir
        self.assertEqual(rooms.gc(max_age_days=7)["members"], 1)
        leave = [ev for ev in store.read_events("r") if ev.get("type") == "leave"]
        self.assertEqual([(ev["sid"], ev["name"]) for ev in leave], [(self.a, "alice")])

    def test_join_tolerates_malformed_members_entries(self):
        store.save_members("r", {self.b: {"name": ["bob"]}, "junk": "not-a-dict"})
        res = rooms.join(self.a, "r", "alice", "/root")
        self.assertEqual(res["members"], ["alice"])

    def test_join_tolerates_malformed_room_meta(self):
        store.save_meta("r", {"display": "r", "aliases_used": "abc"})
        res = rooms.join(self.a, "r", "alice", "/root")
        self.assertEqual((res["alias"], res["root"]), ("a", "/root"))

    def test_gc_ignores_non_ascii_digit_pid_files(self):
        by_pid = os.path.join(self.home, "sessions", "by-pid")
        os.makedirs(by_pid)
        with open(os.path.join(by_pid, "\u00b2.json"), "w") as fh:
            fh.write("{}")
        self.assertEqual(rooms.gc()["pids"], 0)

    # -- C2: rename checks and writes under the room locks -------------------

    def _make_running(self, sid):
        sessions.write_by_pid(os.getpid(), {"sid": sid, "pid_started_at": sessions.pid_started_at(os.getpid())})

    def test_rename_rechecks_the_clash_under_the_lock(self):
        """A running session takes the name right after rename's first look: rename must refuse."""
        rooms.join(self.a, "r", "alice", "/root")
        self._make_running(self.b)
        real_load = store.load_members
        calls = []

        def load_then_bob_joins(room):
            members = real_load(room)
            calls.append(room)
            if len(calls) == 1:
                late = dict(members)
                late[self.b] = {"name": "bob", "alias": "b", "joined_at": 0.0, "root": "/root"}
                store.save_members(room, late)
            return members

        with mock.patch.object(store, "load_members", load_then_bob_joins):
            self.assertFalse(rooms.rename(self.a, ["r"], "bob"))
        self.assertEqual(store.load_members("r")[self.a]["name"], "alice")

    def test_rename_is_all_or_nothing_when_a_room_lock_is_busy(self):
        rooms.join(self.a, "r1", "alice", "/root")
        rooms.join(self.a, "r2", "alice", "/root")
        with mock.patch.object(rooms, "RENAME_LOCK_TIMEOUT", 0.05):
            with store.room_lock("r2"):
                self.assertFalse(rooms.rename(self.a, ["r1", "r2"], "alicia"))
        self.assertEqual(store.load_members("r1")[self.a]["name"], "alice")
        self.assertEqual(store.load_members("r2")[self.a]["name"], "alice")
        self.assertTrue(rooms.rename(self.a, ["r1", "r2"], "alicia"))
        self.assertEqual(store.load_members("r2")[self.a]["name"], "alicia")

    # -- C3: gc tolerates files a concurrent gc or carry_over removed --------

    def test_gc_tolerates_a_by_pid_file_removed_concurrently(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        sessions.write_by_pid(proc.pid, {"sid": "not-a-sid"})
        real_alive = sessions.pid_alive

        def alive_after_another_gc(pid):
            os.unlink(sessions.by_pid_path(pid))
            return real_alive(pid)

        with mock.patch.object(sessions, "pid_alive", alive_after_another_gc):
            self.assertEqual(rooms.gc()["pids"], 0)

    def test_gc_tolerates_a_session_dir_removed_concurrently(self):
        os.makedirs(paths.session_dir(self.a))

        def gone_by_now(sid, now=None):
            shutil.rmtree(paths.session_dir(sid))
            return None

        with mock.patch.object(sessions, "active_age", gone_by_now):
            self.assertEqual(rooms.gc()["sessions"], 0)

    # -- C4: no `ps` while holding a room lock --------------------------------

    def _record_process_state_under_room_lock(self, room_names):
        real_state = sessions.process_state
        under_lock = []

        def probe(sid):
            for room in room_names:
                try:
                    with paths.FileLock(os.path.join(paths.room_dir(room), ".lock"), blocking=False):
                        pass
                except paths.LockBusy:
                    under_lock.append((room, sid))
            return real_state(sid)

        patcher = mock.patch.object(sessions, "process_state", probe)
        patcher.start()
        self.addCleanup(patcher.stop)
        return under_lock

    def test_join_rename_and_gc_check_processes_outside_the_room_lock(self):
        rooms.join(self.a, "r", "alice", "/root")
        rooms.join(self.b, "r", "bob", "/root")
        self._make_running(self.b)
        c = new_sid()
        under_lock = self._record_process_state_under_room_lock(["r"])
        with self.assertRaises(paths.PassnoteError):
            rooms.join(c, "r", "bob", "/root")  # clash with running bob
        self.assertFalse(rooms.rename(self.a, ["r"], "bob"))
        rooms.gc()
        self.assertEqual(under_lock, [])

    def test_sweep_keeps_a_member_whose_session_dir_appeared_before_it_took_the_lock(self):
        """A join in progress: the member is in members.json before its session dir exists. The
        sweep decides under the room lock, so a join that finished meanwhile keeps its member."""
        store.save_members("r", {self.a: {"name": "alice", "alias": "a", "joined_at": 0.0, "root": "/root"}})
        real_lock = store.room_lock

        def lock_after_the_join_finishes(room, *args, **kwargs):
            os.makedirs(paths.session_dir(self.a), exist_ok=True)
            return real_lock(room, *args, **kwargs)

        with mock.patch.object(store, "room_lock", lock_after_the_join_finishes):
            rooms.gc()
        self.assertIn(self.a, store.load_members("r"))

    def test_sweep_leaves_a_gone_member_with_a_session_dir_to_the_age_rule(self):
        """The sweep reclaims orphans only (no session dir, or not a session id): a gone member
        with its session dir is the first pass's to judge, by age."""
        rooms.join(self.a, "r", "alice", "/root")
        sessions.save_meta(self.a, dict(sessions.load_meta(self.a), rooms=[]))  # not in its meta's rooms
        self._make_gone(self.a)
        sessions.touch_active(self.a)
        rooms.gc(max_age_days=7)
        self.assertEqual(list(store.load_members("r")), [self.a])

    def test_carry_over_preserves_pid(self):
        rooms.join(self.a, "r", "alice", "/root")
        started = sessions.pid_started_at(os.getpid())
        sessions.write_by_pid(os.getpid(), {"sid": self.a, "pid_started_at": started})
        new = new_sid()
        rooms.carry_over(self.a, new)
        new_meta = sessions.load_meta(new)
        self.assertEqual(new_meta["pid"], os.getpid())
        self.assertEqual(new_meta["pid_started_at"], started)


if __name__ == "__main__":
    unittest.main()
