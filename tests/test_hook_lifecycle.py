import json
import os
import unittest
from unittest import mock

from support import HomeCase, hook_input, join, new_sid, post
from passnote import hook, paths, rooms, sessions, store


class LifecycleTest(HomeCase):
    def setUp(self):
        super().setUp()
        paths.ensure_home()
        self.a, self.b = new_sid(), new_sid()
        join(self.a, "r", "alice")
        join(self.b, "r", "bob")
        self.pid = str(os.getpid())

    def run_hook(self, event, sid, **fields):
        data = {"session_id": sid, "hook_event_name": event}
        data.update(fields)
        out = hook.main(event, json.dumps(data), self.env(sid, CLAUDE_PID=self.pid))
        return json.loads(out) if out else None

    def test_clear_carries_membership_to_new_session(self):
        self.assertIsNone(self.run_hook("SessionEnd", self.b, reason="clear"))
        new = new_sid()
        out = self.run_hook("SessionStart", new, source="clear")
        self.assertIn(new, store.load_members("r"))
        self.assertNotIn(self.b, store.load_members("r"))
        self.assertEqual(sessions.load_meta(new)["rooms"], ["r"])
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"],
                         "passnote: you are bob in rooms r; /passnote for the protocol")
        post(self.a, "r", "after clear")
        self.assertIn("after clear", hook.main("PostToolBatch", hook_input(new), self.env(new)))

    def test_pending_receipts_survive_clear(self):
        # alice (self.a) has a pending receipt; a /clear carries it to the new sid
        post(self.a, "r", "q", kind="ask", to=["bob"])
        hook.main("PostToolBatch", hook_input(self.a), self.env(self.a))
        new = new_sid()
        rooms.carry_over(self.a, new)
        self.assertEqual([entry["id"] for entry in sessions.load_emit(new)["receipts"]], ["a1"])

    def test_delivery_evidence_survives_clear(self):
        post(self.a, "r", "q", kind="ask", to=["bob"])
        hook.main("PostToolBatch", hook_input(self.b), self.env(self.b))
        new = new_sid()
        rooms.carry_over(self.b, new)
        self.assertEqual([entry["id"] for entry in sessions.load_emit(new)["delivered"]], ["a1"])

    def test_reminder_lists_at_most_five_rooms(self):
        for i in range(2, 9):
            join(self.b, f"r{i}", "bob")
        out = self.run_hook("SessionStart", self.b, source="resume")
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"],
                         "passnote: you are bob in rooms r, r2, r3, r4, r5 +3; /passnote for the protocol")

    def test_clear_carries_once(self):
        self.run_hook("SessionEnd", self.b, reason="clear")
        new = new_sid()
        self.run_hook("SessionStart", new, source="clear")
        other = new_sid()
        self.assertIsNone(self.run_hook("SessionStart", other, source="clear"))
        self.assertIn(new, store.load_members("r"))
        self.assertNotIn(other, store.load_members("r"))

    def test_clear_keeps_the_overflow_for_the_new_session(self):
        # Back-pressure leaves refs in the emit state that are already behind the cursor.
        ids = {post(self.a, "r", f"{i}:" + "x" * 400)["id"] for i in range(10)}
        first = hook.main("PostToolBatch", hook_input(self.b), self.env(self.b))
        self.assertIn("not shown yet:", first)
        self.assertTrue(sessions.load_emit(self.b)["overflow"])
        self.run_hook("SessionEnd", self.b, reason="clear")
        new = new_sid()
        self.run_hook("SessionStart", new, source="clear")
        seen = {i for i in ids if i + " " in first}
        for _ in range(10):
            out = hook.main("PostToolBatch", hook_input(new), self.env(new))
            seen |= {i for i in ids if i + " " in out}
        self.assertEqual(seen, ids)

    def test_a_failed_carry_over_is_finished_by_the_next_delivery_fire(self):
        join(self.b, "r2", "bob")
        self.run_hook("SessionEnd", self.b, reason="clear")
        new = new_sid()
        real = store.room_lock

        def flaky(room, *args, **kwargs):
            if room == "r2":
                raise paths.LockBusy
            return real(room, *args, **kwargs)

        with mock.patch.object(store, "room_lock", flaky):
            self.assertIsNone(self.run_hook("SessionStart", new, source="clear"))
        self.assertEqual(sessions.load_meta(new)["rooms"], [])  # stranded half way
        post(self.a, "r", "one")
        out = hook.main("PostToolBatch", hook_input(new), self.env(new, CLAUDE_PID=self.pid))
        self.assertIn("one", out)
        self.assertEqual(sorted(sessions.load_meta(new)["rooms"]), ["r", "r2"])
        self.assertIn(new, store.load_members("r2"))
        self.assertNotIn(self.b, store.load_members("r2"))
        self.assertNotIn("prev_sid", sessions.read_by_pid(self.pid))

    def test_a_busy_lock_is_retried_once_inside_session_start(self):
        self.run_hook("SessionEnd", self.b, reason="clear")
        real, calls = store.room_lock, []

        def once_busy(room, *args, **kwargs):
            calls.append(room)
            if len(calls) == 1:
                raise paths.LockBusy
            return real(room, *args, **kwargs)

        new = new_sid()
        with mock.patch.object(store, "room_lock", once_busy):
            out = self.run_hook("SessionStart", new, source="clear")
        self.assertIn("you are bob", out["hookSpecificOutput"]["additionalContext"])

    def test_delivery_for_an_unjoined_session_reads_only_the_by_pid_record(self):
        sid = new_sid()
        with mock.patch.object(sessions, "pid_started_at") as token:
            out = hook.main("PostToolBatch", hook_input(sid), self.env(sid, CLAUDE_PID=self.pid))
        self.assertEqual(out, "")
        token.assert_not_called()
        self.assertFalse(os.path.exists(paths.session_dir(sid)))

    def test_a_second_clear_before_any_fire_still_carries(self):
        self.run_hook("SessionEnd", self.b, reason="clear")
        mid, last = new_sid(), new_sid()
        with mock.patch.object(store, "room_lock", side_effect=paths.LockBusy):
            self.assertIsNone(self.run_hook("SessionStart", mid, source="clear"))  # the carry fails
        self.run_hook("SessionEnd", mid, reason="clear")  # cleared again before any fire
        out = self.run_hook("SessionStart", last, source="clear")
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"],
                         "passnote: you are bob in rooms r; /passnote for the protocol")
        members = store.load_members("r")
        self.assertIn(last, members)
        self.assertNotIn(self.b, members)
        self.assertNotIn(mid, members)

    def test_pid_reuse_guard_blocks_carry_over(self):
        self.run_hook("SessionEnd", self.b, reason="clear")
        record = sessions.read_by_pid(self.pid)
        record["pid_started_at"] = "Thu Jan  1 00:00:00 1970"
        sessions.write_by_pid(self.pid, record)
        new = new_sid()
        self.assertIsNone(self.run_hook("SessionStart", new, source="clear"))
        self.assertIn(self.b, store.load_members("r"))

    def test_session_end_keeps_the_prev_sid_when_its_heal_is_busy(self):
        self.run_hook("SessionEnd", self.b, reason="clear")
        mid, last = new_sid(), new_sid()
        with mock.patch.object(store, "room_lock", side_effect=paths.LockBusy):
            self.run_hook("SessionStart", mid, source="clear")
            self.run_hook("SessionEnd", mid, reason="clear")
        self.assertEqual(sessions.read_by_pid(self.pid)["prev_sid"], self.b)
        self.run_hook("SessionStart", last, source="clear")
        self.assertIn(last, store.load_members("r"))

    def _stranded(self):
        """bob cleared; SessionStart(clear) moved room r but not r2 (busy): a half-carried session."""
        join(self.b, "r2", "bob")
        self.run_hook("SessionEnd", self.b, reason="clear")
        new = new_sid()
        real = store.room_lock

        def flaky(room, *args, **kwargs):
            if room == "r2":
                raise paths.LockBusy
            return real(room, *args, **kwargs)

        with mock.patch.object(store, "room_lock", flaky):
            self.run_hook("SessionStart", new, source="clear")
        return new

    def test_a_heal_waits_for_the_delivery_lock(self):
        new = self._stranded()
        post(self.a, "r", "one")
        fire = lambda: hook.main("PostToolBatch", hook_input(new), self.env(new, CLAUDE_PID=self.pid))  # noqa: E731
        with sessions.session_lock(new):
            self.assertEqual(fire(), "")
        self.assertEqual(sessions.load_meta(new)["rooms"], [])
        self.assertIn("one", fire())

    def test_session_start_carry_waits_for_the_delivery_lock(self):
        self.run_hook("SessionEnd", self.b, reason="clear")
        new = new_sid()
        with sessions.session_lock(new):
            self.assertIsNone(self.run_hook("SessionStart", new, source="clear"))
        self.assertIn(self.b, store.load_members("r"))
        self.assertEqual(sessions.read_by_pid(self.pid)["prev_sid"], self.b)

    def _once_busy(self, timeouts):
        real = store.room_lock

        def once_busy(room, *args, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            if len(timeouts) == 1:
                raise paths.LockBusy
            return real(room, *args, **kwargs)

        return once_busy

    def test_a_retry_runs_when_a_whole_retry_still_fits_the_budget(self):
        self.run_hook("SessionEnd", self.b, reason="clear")
        timeouts, clock = [], iter([0.0, 2.1])
        new = new_sid()
        with mock.patch.object(store, "room_lock", self._once_busy(timeouts)), \
                mock.patch.object(hook, "_clock", lambda: next(clock)):
            out = self.run_hook("SessionStart", new, source="clear")
        self.assertIn("you are bob", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(timeouts[0], rooms.CARRY_LOCK_TIMEOUT)
        self.assertAlmostEqual(timeouts[1], hook.CARRY_BUDGET - 2.1 - sessions.META_LOCK_TIMEOUT)

    def test_no_retry_once_the_budget_is_spent(self):
        self.run_hook("SessionEnd", self.b, reason="clear")
        timeouts, clock = [], iter([0.0, 3.1])
        new = new_sid()
        with mock.patch.object(store, "room_lock", self._once_busy(timeouts)), \
                mock.patch.object(hook, "_clock", lambda: next(clock)):
            self.assertIsNone(self.run_hook("SessionStart", new, source="clear"))
        self.assertEqual(len(timeouts), 1)
        self.assertIn(self.b, store.load_members("r"))

    def test_a_mismatched_start_token_drops_the_prev_sid(self):
        self.run_hook("SessionEnd", self.b, reason="clear")
        record = sessions.read_by_pid(self.pid)
        record["pid_started_at"] = "Thu Jan  1 00:00:00 1970"
        paths.atomic_write_json(sessions.by_pid_path(self.pid), record)
        new = new_sid()
        hook.main("PostToolBatch", hook_input(new), self.env(new, CLAUDE_PID=self.pid))
        self.assertNotIn("prev_sid", sessions.read_by_pid(self.pid))
        self.assertIn(self.b, store.load_members("r"))

    def test_a_failed_start_token_lookup_keeps_the_prev_sid(self):
        self.run_hook("SessionEnd", self.b, reason="clear")
        new = new_sid()
        with mock.patch.object(sessions, "pid_started_at", return_value=None):
            hook.main("PostToolBatch", hook_input(new), self.env(new, CLAUDE_PID=self.pid))
        self.assertEqual(sessions.read_by_pid(self.pid)["prev_sid"], self.b)

    def test_a_garbled_prev_sid_is_dropped_without_an_error(self):
        paths.atomic_write_json(sessions.by_pid_path(self.pid),
                                {"sid": self.b, "pid_started_at": sessions.pid_started_at(self.pid), "prev_sid": "../x"})
        new = new_sid()
        self.assertEqual(hook.main("PostToolBatch", hook_input(new), self.env(new, CLAUDE_PID=self.pid)), "")
        self.assertNotIn("prev_sid", sessions.read_by_pid(self.pid))
        self.assertFalse(os.path.exists(os.path.join(self.home, "errors.log")))

    def test_the_healing_fire_says_who_you_are(self):
        new = self._stranded()
        post(self.a, "r", "one")
        out = json.loads(hook.main("PostToolBatch", hook_input(new), self.env(new, CLAUDE_PID=self.pid)))
        lines = out["hookSpecificOutput"]["additionalContext"].split("\n")
        self.assertIn("passnote: you are bob in rooms r, r2; /passnote for the protocol", lines)
        self.assertTrue(any(line.endswith("say: one") for line in lines))
        # The next fire doesn't repeat it.
        post(self.a, "r", "two")
        again = hook.main("PostToolBatch", hook_input(new), self.env(new, CLAUDE_PID=self.pid))
        self.assertNotIn("you are bob", again)

    def test_the_reminder_has_a_fixed_shape_when_it_would_be_too_large(self):
        for i in range(5):
            room = f"big{i}"
            join(self.b, room, "bob")
            meta = store.load_meta(room)
            meta["display"] = "\U0001F600" * 64
            store.save_meta(room, meta)
        line = hook._reminder(self.b, sessions.load_meta(self.b))
        self.assertLessEqual(len(json.dumps(line)) - 2, hook.REMINDER_MAX_JSON)
        self.assertEqual(line, "passnote: you are a member of 6 room(s); passnote rooms lists them; "
                               "/passnote for the protocol")

    def test_session_end_records_the_pid_with_the_process_start_token(self):
        self.run_hook("SessionEnd", self.b, reason="clear")
        self.assertEqual(sessions.read_by_pid(self.pid), {
            "sid": self.b, "prev_sid": self.b, "pid_started_at": sessions.pid_started_at(self.pid)})

    def test_resume_reminds_and_records(self):
        post(self.a, "r", "can you review?", kind="ask", to=["bob"])
        out = self.run_hook("SessionStart", self.b, source="resume",
                            session_title="Bob Session", transcript_path="/tmp/t.jsonl")
        self.assertIn("Waiting on your reply: a1", out["hookSpecificOutput"]["additionalContext"])
        meta = sessions.load_meta(self.b)
        self.assertEqual((meta["title"], meta["transcript_path"]), ("Bob Session", "/tmp/t.jsonl"))
        self.assertEqual(sessions.read_by_pid(self.pid)["sid"], self.b)

    def test_resume_does_not_list_an_answered_ask(self):
        ask = post(self.a, "r", "can you review?", kind="ask", to=["bob"])
        post(self.b, "r", "done", re=ask["id"])
        out = self.run_hook("SessionStart", self.b, source="resume")
        self.assertNotIn("Waiting on your reply", out["hookSpecificOutput"]["additionalContext"])

    def test_pid_mirror_survives_a_title_change(self):
        # update_meta first, write_by_pid after: a stale meta can't overwrite the mirrored pid.
        self.run_hook("SessionStart", self.b, source="resume", session_title="Renamed", transcript_path="/tmp/t")
        meta = sessions.load_meta(self.b)
        self.assertEqual(meta["title"], "Renamed")
        self.assertEqual(meta["pid"], int(self.pid))
        self.assertEqual(meta["pid_started_at"], sessions.pid_started_at(self.pid))

    def test_a_garbled_pid_is_ignored(self):
        data = json.dumps({"session_id": self.b, "source": "resume"})
        for bad in ("12abc", "0", "-1", " 7"):
            out = hook.main("SessionStart", data, self.env(self.b, CLAUDE_PID=bad))
            self.assertIn("passnote: you are bob", out)
        self.assertIsNone(sessions.load_meta(self.b)["pid"])

    def test_startup_and_fork_inject_nothing(self):
        self.assertIsNone(self.run_hook("SessionStart", self.b, source="startup"))
        fork = new_sid()
        self.assertIsNone(self.run_hook("SessionStart", fork, source="fork", session_title="bob"))
        self.assertNotIn(fork, store.load_members("r"))

    def test_unjoined_session_gets_no_files(self):
        sid = new_sid()
        self.assertIsNone(self.run_hook("SessionStart", sid, source="resume", session_title="x"))
        self.assertFalse(os.path.exists(paths.session_dir(sid)))

    def test_subagent_lifecycle_hooks_do_nothing(self):
        self.assertIsNone(self.run_hook("SessionEnd", self.b, reason="clear", agent_id="sub-1"))
        self.assertIsNone(sessions.read_by_pid(self.pid))
        self.assertIsNone(self.run_hook("SessionStart", self.b, source="resume", agent_id="sub-1"))

    def test_session_end_other_reasons_do_nothing(self):
        self.run_hook("SessionEnd", self.b, reason="logout")
        self.assertIsNone(sessions.read_by_pid(self.pid))

    def test_lock_busy_exits_silently_without_an_error_notice(self):
        busy = mock.Mock(side_effect=paths.LockBusy)
        with mock.patch.object(sessions, "record_pid", busy):
            self.assertIsNone(self.run_hook("SessionEnd", self.b, reason="clear"))
            self.assertIsNone(self.run_hook("SessionStart", self.b, source="resume"))
        self.run_hook("SessionEnd", self.b, reason="clear")
        with mock.patch.object(rooms, "carry_over", busy):
            self.assertIsNone(self.run_hook("SessionStart", new_sid(), source="clear"))
        with mock.patch.object(sessions, "update_meta", busy):
            self.assertIsNone(self.run_hook("SessionStart", self.b, source="resume"))
        self.assertFalse(os.path.exists(os.path.join(paths.session_dir(self.b), ".error-shown")))

    def test_an_error_is_logged_and_never_raised(self):
        with mock.patch.object(sessions, "load_meta", side_effect=OSError):
            out = hook.main("SessionStart", json.dumps({"session_id": self.b}), self.env(self.b))
        self.assertEqual(out, "")
        with open(os.path.join(paths.home(), "errors.log")) as fh:
            self.assertIn("OSError", fh.read())

    def test_pre_tool_use_denies_subagent_writes_only(self):
        def run(command, agent_id="sub-1"):
            data = {"session_id": self.b, "tool_name": "Bash", "tool_input": {"command": command}}
            if agent_id:
                data["agent_id"] = agent_id
            return hook.main("PreToolUse", json.dumps(data), self.env(self.b))
        denied = json.loads(run("printf hi | passnote post --to alice"))
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(run("cd x && passnote claim 'docs'"))
        self.assertEqual(run("passnote read --last 5"), "")
        self.assertEqual(run("echo passnote-post"), "")
        self.assertEqual(run("printf hi | passnote post", agent_id=None), "")

    def test_pre_tool_use_command_forms(self):
        denied = [
            "passnote post", "passnote  join --as x", "passnote leave", "passnote claim 'docs'",
            "/abs/path/passnote join", "~/.local/bin/passnote post", "python3 /x/bin/passnote post",
            'bash -c "passnote post"', "sh -c 'passnote claim docs'", "echo hi; passnote post",
            "true && passnote post", "echo hi | passnote post", "echo $(passnote post)",
            "echo `passnote post`", "x\npassnote post", "\\passnote post", "(passnote post)",
            'passnote "post"', "passnote 'claim' x", '"passnote" post', "'passnote' join",
            "passnote -- post", "passnote  --  claim x", "passnote -- 'join'", "echo hi | passnote -- post",
            "passnote subscribe auth", "passnote unsubscribe auth", "passnote digest on", "passnote subscribe --all",
        ]
        allowed = [
            "passnote read", "passnote who", "passnote watch", "passnote rooms", "passnote doctor",
            "mypassnote post", "passnote-post", "echo passnote", "passnote", "passnote posting",
            "passnote post-mortem", "passnote.post", "passnote\npost", "passnote  'read'", "ls /x/passnote/post", "", "git status",
            "passnote -- read", "passnote --version", "passnote --post",
            "passnote subscribers", "passnote digest-x", "passnote subscribe-x",
        ]
        for command in denied:
            with self.subTest(deny=command):
                self.assertIsNotNone(hook._WRITE_CMD.search(command))
        for command in allowed:
            with self.subTest(allow=command):
                self.assertIsNone(hook._WRITE_CMD.search(command))

    def test_pre_tool_use_ignores_other_tools_and_bad_input(self):
        def run(**data):
            data = dict({"session_id": self.b, "agent_id": "sub-1"}, **data)
            return hook.main("PreToolUse", json.dumps(data), self.env(self.b))
        self.assertEqual(run(tool_name="Write", tool_input={"command": "passnote post"}), "")
        self.assertEqual(run(tool_name="Bash", tool_input={"command": ["passnote", "post"]}), "")
        self.assertEqual(run(tool_name="Bash", tool_input="passnote post"), "")
        self.assertEqual(run(tool_name="Bash"), "")


if __name__ == "__main__":
    unittest.main()
