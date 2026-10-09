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

    def test_session_end_of_an_unjoined_session_clears_a_stale_prev_sid(self):
        self.run_hook("SessionEnd", self.b, reason="clear")
        unjoined = new_sid()
        self.run_hook("SessionEnd", unjoined, reason="clear")
        self.assertNotIn("prev_sid", sessions.read_by_pid(self.pid))
        self.assertIsNone(self.run_hook("SessionStart", new_sid(), source="clear"))
        self.assertIn(self.b, store.load_members("r"))

    def test_pid_reuse_guard_blocks_carry_over(self):
        self.run_hook("SessionEnd", self.b, reason="clear")
        record = sessions.read_by_pid(self.pid)
        record["pid_started_at"] = "Thu Jan  1 00:00:00 1970"
        sessions.write_by_pid(self.pid, record)
        new = new_sid()
        self.assertIsNone(self.run_hook("SessionStart", new, source="clear"))
        self.assertIn(self.b, store.load_members("r"))

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
        ]
        allowed = [
            "passnote read", "passnote who", "passnote watch", "passnote rooms", "passnote doctor",
            "mypassnote post", "passnote-post", "echo passnote", "passnote", "passnote posting",
            "passnote post-mortem", "passnote.post", "passnote\npost", "passnote  'read'", "ls /x/passnote/post", "", "git status",
            "passnote -- read", "passnote --version", "passnote --post",
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
