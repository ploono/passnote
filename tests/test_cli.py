import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from support import BIN, CliCase, new_sid
from passnote import cli, cursor, paths, rooms, sessions, store


def set_mode(sid, mode):
    sessions.update_meta(sid, lambda meta: meta.update(permission_mode=mode))


class JoinTest(CliCase):
    def test_join_explicit_room_as_name(self):
        code, out, err = self.run_cli(self.a, "join", "r", "--as", "alice")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("joined r (r) as alice\nroot: "))
        self.assertIn("members: alice", out)
        self.assertEqual(sessions.load_meta(self.a)["name_source"], "as")

    def test_join_lists_a_gone_member_apart(self):
        self.run_cli(self.b, "join", "r", "--as", "bob")
        self.run_cli(self.c, "join", "r", "--as", "carol")
        sessions.update_meta(self.b, lambda meta: meta.update(pid=2 ** 31 - 2))  # no such process
        code, out, err = self.run_cli(self.a, "join", "r", "--as", "alice")
        self.assertEqual(code, 0, err)
        self.assertIn("\nmembers: alice, carol (gone: bob)\n", out)

    def test_join_without_gone_members_lists_them_all(self):
        self.run_cli(self.b, "join", "r", "--as", "bob")
        code, out, err = self.run_cli(self.a, "join", "r", "--as", "alice")
        self.assertEqual(code, 0, err)
        self.assertIn("\nmembers: alice, bob\n", out)

    def test_join_default_room_from_git_repo(self):
        repo = os.path.join(self.tmp, "my-proj")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q", repo], check=True)
        cwd = os.getcwd()
        os.chdir(repo)
        self.addCleanup(os.chdir, cwd)
        code, out, _ = self.run_cli(self.a, "join", "--as", "alice")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"^joined my-proj \(my-proj-[0-9a-f]{4}\) as alice")

    def test_join_needs_a_name(self):
        code, _, err = self.run_cli(self.a, "join", "r")
        self.assertEqual(code, 2)
        self.assertIn("--as", err)

    def test_join_uses_user_set_registry_name_and_records_pid(self):
        reg = os.path.join(self.claude_home, "sessions")
        os.makedirs(reg)
        with open(os.path.join(reg, f"{os.getpid()}.json"), "w") as fh:
            json.dump({"name": "session-a", "nameSource": "user"}, fh)
        code, out, _ = self.run_cli(self.a, "join", "r", CLAUDE_PID=os.getpid())
        self.assertEqual(code, 0)
        self.assertIn("as session-a", out)
        self.assertEqual(sessions.read_by_pid(os.getpid())["sid"], self.a)
        self.assertEqual(sessions.load_meta(self.a)["pid"], os.getpid())

    def test_garbled_claude_pid_is_ignored(self):
        code, _, err = self.run_cli(self.a, "join", "r", "--as", "alice", CLAUDE_PID="12²")
        self.assertEqual(code, 0, err)
        self.assertIsNone(sessions.load_meta(self.a)["pid"])

    def test_not_in_a_session(self):
        code, _, err = self.run_cli(None, "join", "r", "--as", "x")
        self.assertEqual(code, 2)
        self.assertIn("CLAUDE_CODE_SESSION_ID", err)

    def test_leave_and_rooms(self):
        self.run_cli(self.a, "join", "r", "--as", "alice")
        self.assertIn("r (r) · 1 member(s)", self.run_cli(self.a, "rooms")[1])
        self.assertEqual(self.run_cli(self.a, "leave")[1], "left r\n")
        self.assertIn("not joined", self.run_cli(self.a, "rooms")[1])
        self.assertEqual(self.run_cli(self.a, "leave", "--room", "r")[0], 3)

    def test_second_room_renames_the_session_everywhere(self):
        self.run_cli(self.a, "join", "r1", "--as", "alice")
        self.run_cli(self.b, "join", "r1", "--as", "bob")
        code, _, err = self.run_cli(self.a, "join", "r2", "--as", "al")
        self.assertEqual(code, 0, err)
        self.assertEqual(store.load_members("r1")[self.a]["name"], "al")
        self.assertEqual(store.load_members("r2")[self.a]["name"], "al")
        set_mode(self.a, "default")
        set_mode(self.b, "default")
        self.run_cli(self.b, "post", "--room", "r1", "--to", "al", stdin="still reach you?")
        self.assertIn("bob→you ", self.run_cli(self.a, "read", "--room", "r1")[1])

    def test_second_room_fails_cleanly_when_the_new_name_is_taken_in_the_first(self):
        self.run_cli(self.a, "join", "r1", "--as", "alice")
        self.run_cli(self.b, "join", "r1", "--as", "al")
        code, _, err = self.run_cli(self.a, "join", "r2", "--as", "al")
        self.assertEqual(code, 2)
        self.assertIn("running session in another room", err)
        self.assertEqual(store.load_members("r1")[self.a]["name"], "alice")
        self.assertNotIn(self.a, store.load_members("r2"))
        self.assertEqual(sessions.load_meta(self.a)["rooms"], ["r1"])

    def test_clash_in_the_new_room_renames_nothing(self):
        self.run_cli(self.a, "join", "r1", "--as", "alice")
        self.run_cli(self.c, "join", "r2", "--as", "al")
        code, _, err = self.run_cli(self.a, "join", "r2", "--as", "al")
        self.assertEqual(code, 2)
        self.assertEqual(store.load_members("r1")[self.a]["name"], "alice")
        self.assertEqual(sessions.load_meta(self.a)["name"], "alice")
        code, _, err = self.run_cli(self.a, "join", "r2", "--as", "alice")
        self.assertEqual(code, 0, err)
        self.assertEqual({store.load_members(r)[self.a]["name"] for r in ("r1", "r2")}, {"alice"})
        self.assertEqual(sessions.load_meta(self.a)["name"], "alice")

    def test_a_failed_join_after_the_rename_restores_the_old_name(self):
        self.run_cli(self.a, "join", "r1", "--as", "alice")
        with mock.patch.object(cli.rooms, "join", side_effect=paths.LockBusy("x")):
            code, _, _ = self.run_cli(self.a, "join", "r2", "--as", "al")
        self.assertEqual(code, 2)
        self.assertEqual(store.load_members("r1")[self.a]["name"], "alice")

    def test_second_join_without_as_keeps_the_session_name(self):
        self.run_cli(self.a, "join", "r1", "--as", "alice")
        code, out, err = self.run_cli(self.a, "join", "r2")
        self.assertEqual(code, 0, err)
        self.assertIn("as alice", out)
        self.assertEqual(sessions.load_meta(self.a)["name_source"], "as")

    def test_leave_takes_the_session_delivery_lock(self):
        self.run_cli(self.a, "join", "r", "--as", "alice")
        with mock.patch.object(cli.sessions, "session_lock", wraps=sessions.session_lock) as lock:
            self.assertEqual(self.run_cli(self.a, "leave")[0], 0)
        lock.assert_called_once_with(self.a, timeout=cli.LEAVE_LOCK_TIMEOUT)

    def test_leave_while_a_hook_fire_holds_the_session_is_a_retry_message(self):
        self.run_cli(self.a, "join", "r", "--as", "alice")
        with mock.patch.object(cli, "LEAVE_LOCK_TIMEOUT", 0.1), sessions.session_lock(self.a):
            code, _, err = self.run_cli(self.a, "leave")
        self.assertEqual(code, 2)
        self.assertIn("busy", err)
        self.assertIn(self.a, store.load_members("r"))
        self.assertEqual(self.run_cli(self.a, "leave")[0], 0)
        self.assertFalse(os.path.exists(cursor.path(self.a, "r")))


class JoinNameStdinTest(CliCase):
    """`join --name-stdin`: what /passnote:join feeds it, the typed name framed as `[<name>]` (#5)."""

    def join_stdin(self, text, *argv, **env):
        return self.run_cli(self.a, "join", "r", "--name-stdin", *argv, stdin=text, **env)

    def assert_refused(self, text, message):
        code, out, err = self.join_stdin(text)
        self.assertEqual(code, 2, (text, out, err))
        self.assertIn(message, err)
        self.assertEqual(out, "")
        self.assertFalse(os.path.exists(os.path.join(paths.room_dir("r"), "members.json")), text)
        self.assertEqual(sessions.load_meta(self.a)["rooms"], [])

    def test_a_framed_name_joins_as_that_name(self):
        for text in ("[bob]\n", "[bob]", "[ bob ]\n", "[\tbob]\n"):
            with self.subTest(text=text):
                code, out, err = self.join_stdin(text)
                self.assertEqual(code, 0, err)
                self.assertTrue(out.startswith("joined r (r) as bob\n"), out)
                self.assertEqual(sessions.load_meta(self.a)["name_source"], "as")

    def test_empty_brackets_resolve_the_name_as_a_bare_join_does(self):
        reg = os.path.join(self.claude_home, "sessions")
        os.makedirs(reg)
        with open(os.path.join(reg, f"{os.getpid()}.json"), "w") as fh:
            json.dump({"name": "session-a", "nameSource": "user"}, fh)
        for text in ("[]\n", "[  ]\n"):
            with self.subTest(text=text):
                code, out, err = self.join_stdin(text, CLAUDE_PID=os.getpid())
                self.assertEqual(code, 0, err)
                self.assertIn("as session-a", out)
                self.assertEqual(sessions.load_meta(self.a)["name_source"], "registry")

    def test_empty_brackets_keep_the_session_name(self):
        self.run_cli(self.a, "join", "r1", "--as", "alice")
        code, out, err = self.join_stdin("[]\n")
        self.assertEqual(code, 0, err)
        self.assertIn("as alice", out)

    def test_a_cut_short_or_multi_line_frame_is_refused(self):
        for text in ("", "\n", "bob\n", "[bob\n", "bob]\n", "[bob]\n\n", "[bob]\n[x]\n", "[bob\n]\n",
                     "[bob] x\n", "x[bob]\n", "[bob]\r\n", "[bo\rb]\n"):
            with self.subTest(text=text):
                self.assert_refused(text, "was cut short or spans lines")

    def test_the_name_goes_through_name_validation(self):
        for name in ("bo'b\"; $(touch M)", "`id`", "a b", "x" * 65, "..", "a]b"):
            with self.subTest(name=name):
                self.assert_refused(f"[{name}]\n", "invalid name")

    def test_a_reserved_name_is_refused(self):
        self.assert_refused("[All]\n", "is reserved")

    def test_as_and_name_stdin_are_exclusive(self):
        code, out, err = self.join_stdin("[bob]\n", "--as", "alice")
        self.assertEqual(code, 2)
        self.assertIn("not allowed with", err)
        self.assertEqual(sessions.load_meta(self.a)["rooms"], [])

    def test_no_name_found_points_at_the_slash_command(self):
        code, _, err = self.join_stdin("[]\n")
        self.assertEqual(code, 2)
        self.assertIn("/passnote:join <name>", err)
        self.assertIn("/rename", err)
        self.assertNotIn("ListAgents", err)
        self.assertNotIn("--as", err)

    def test_without_name_stdin_the_no_name_error_is_unchanged(self):
        code, _, err = self.run_cli(self.a, "join", "r")
        self.assertEqual(code, 2)
        self.assertIn("pass --as <name> (use your ListAgents name)", err)


class PostTest(CliCase):
    def setUp(self):
        super().setUp()
        self.run_cli(self.a, "join", "r", "--as", "alice")
        self.run_cli(self.b, "join", "r", "--as", "bob")
        for sid in (self.a, self.b):  # as their hooks record it: same class, so wakes are allowed
            set_mode(sid, "default")

    def test_not_joined(self):
        self.assertEqual(self.run_cli(self.c, "post", "--room", "r", stdin="hi")[0], 3)
        self.assertEqual(self.run_cli(self.c, "post", stdin="hi")[0], 3)

    def test_broadcast(self):
        set_mode(self.a, None)  # no hook has recorded alice's mode yet: the post is stamped "unknown"
        code, out, _ = self.run_cli(self.a, "post", stdin="hello all\n")
        self.assertEqual((code, out), (0, "ok a1\n"))
        msg = store.iter_messages("r")[-1][1]
        self.assertEqual((msg["text"], msg["to"], msg["kind"], msg["mode"]), ("hello all", "all", "say", "unknown"))

    def test_ask_waits_when_cold_and_wakes_when_warm(self):
        _, out, _ = self.run_cli(self.a, "post", "--to", "bob", "--kind", "ask", stdin="review?")
        self.assertEqual(out, "ok a1\nWAIT bob cold (never active; --urgent to force)\n")
        self.assertEqual(store.read_events("r")[-1]["decision"], "WAIT")
        sessions.touch_active(self.b)
        _, out, _ = self.run_cli(self.a, "post", "--to", "bob", "--kind", "ask", stdin="again?")
        self.assertIn('WAKE bob: SendMessage(to="bob", message="a2 from alice: passnote note waiting")', out)
        self.assertNotIn("again?", out.split("\n", 1)[1])
        self.assertEqual(store.read_events("r")[-1]["decision"], "WAKE")

    def assert_held_wait(self, out, why):
        self.assertEqual(out.splitlines()[1:], [f"WAIT bob held ({why})"])
        self.assertNotIn("WAKE", out)
        self.assertNotIn("SendMessage", out)
        event = store.read_events("r")[-1]
        self.assertEqual((event["type"], event["decision"], event["to"]), ("wake", "WAIT", "bob"))
        self.assertEqual(event["reason"], f"held ({why})")

    def test_a_warm_recipient_in_another_permission_class_is_not_woken(self):
        set_mode(self.b, "bypassPermissions")
        sessions.touch_active(self.b)
        for flags in ((), ("--urgent",)):
            _, out, _ = self.run_cli(self.a, "post", "--to", "bob", "--kind", "ask", *flags, stdin="secret plan?")
            self.assert_held_wait(out, "different permission class; only the human sees it unless the receiver allows bypass")

    def test_a_recipient_whose_mode_is_not_recorded_is_not_woken(self):
        set_mode(self.b, None)
        sessions.touch_active(self.b)
        _, out, _ = self.run_cli(self.a, "post", "--to", "bob", "--kind", "ask", stdin="secret plan?")
        self.assert_held_wait(out, "its permission mode is not recorded yet; "
                                   "it is delivered on their next turn if your classes match")

    def test_room_inbound_refuse_wakes_nobody(self):
        paths.atomic_write_json(os.path.join(paths.room_dir("r"), "config.json"), {"inbound": "refuse"})
        sessions.touch_active(self.b)
        _, out, _ = self.run_cli(self.a, "post", "--to", "bob", "--kind", "ask", stdin="secret plan?")
        self.assert_held_wait(out, "inbound=refuse; only the human sees it")

    def test_a_sender_whose_mode_is_not_recorded_rings_no_doorbell(self):
        # A post in the same command as join: stamped "unknown", and no hook has recorded the mode
        # yet. Its class is unknown, so no doorbell either way, and no claim that the classes differ.
        set_mode(self.a, None)
        sessions.touch_active(self.b)
        for mode in ("bypassPermissions", "default"):
            with self.subTest(receiver=mode):
                set_mode(self.b, mode)
                _, out, _ = self.run_cli(self.a, "post", "--to", "bob", "--kind", "ask", stdin="secret plan?")
                self.assert_held_wait(out, "your permission mode is not recorded yet (join and post in separate turns); no doorbell")

    def test_urgent_wakes_a_cold_session(self):
        _, out, _ = self.run_cli(self.a, "post", "--to", "bob", "--urgent", stdin="now")
        self.assertIn("WAKE bob:", out)

    def test_a_gone_warm_member_waits_even_when_urgent(self):
        sessions.update_meta(self.b, lambda meta: meta.update(pid=2 ** 31 - 2))  # no such process
        sessions.touch_active(self.b)
        for flags in ((), ("--urgent",)):
            _, out, _ = self.run_cli(self.a, "post", "--to", "bob", "--kind", "ask", *flags, stdin="now?")
            self.assertEqual(out.splitlines()[1:], ["WAIT bob gone (session not running; it will see this when resumed)"])
            self.assertEqual(store.read_events("r")[-1]["decision"], "WAIT")

    def test_reply_to_own_ask_is_wake_eligible(self):
        self.run_cli(self.b, "post", "--to", "alice", "--kind", "ask", stdin="ok to merge?")
        sessions.touch_active(self.b)
        _, out, _ = self.run_cli(self.a, "post", "--to", "bob", "--kind", "ans", "--re", "b1", stdin="yes")
        self.assertIn("WAKE bob:", out)
        _, out, _ = self.run_cli(self.a, "post", "--to", "bob", stdin="fyi")
        self.assertEqual(out.splitlines(), ["ok a3"])

    def test_after_clear_earlier_claims_asks_and_messages_stay_the_members(self):
        self.run_cli(self.b, "claim", "the migration")
        self.run_cli(self.b, "post", "--to", "alice", "--kind", "ask", stdin="ok to merge?")
        new_b = new_sid()
        rooms.carry_over(self.b, new_b)  # what /clear does
        sessions.touch_active(new_b)
        self.assertIn("claim b1 bob: the migration", self.run_cli(None, "who", "--room", "r")[1])
        _, out, _ = self.run_cli(None, "read", "--room", "r")
        self.assertIn("b1 bob→all claim: the migration", out)
        self.assertNotIn("(unverified)", out)
        _, out, _ = self.run_cli(self.a, "post", "--to", "bob", "--kind", "ans", "--re", "b2", stdin="yes")
        self.assertIn("WAKE bob:", out)

    def test_a_forged_log_line_is_not_a_reply_target(self):
        # b1 exists in the log but is not a valid message (seq missing): an ans re it is not eligible.
        path = store.log_path("r")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "ab") as fh:
            fh.write(json.dumps({"id": "b1", "from": "bob", "sid": self.b, "kind": "ask", "text": "x",
                                 "to": ["alice"]}).encode() + b"\n")
        sessions.touch_active(self.b)
        _, out, _ = self.run_cli(self.a, "post", "--to", "bob", "--kind", "ans", "--re", "b1", stdin="yes")
        self.assertEqual(out.splitlines()[1:], [])

    def test_breaker(self):
        sessions.touch_active(self.b)
        outs = [self.run_cli(self.a, "post", "--to", "bob", "--kind", "ask", stdin=f"q{i}")[1] for i in range(4)]
        self.assertIn("WAKE bob", outs[2])
        self.assertIn("WAIT bob breaker (3 wakes in 10m)", outs[3])
        self.assertNotIn("QUEUED", "".join(outs))

    def test_errors(self):
        self.assertEqual(self.run_cli(self.a, "post", stdin="x" * 100001)[0], 4)
        code, _, err = self.run_cli(self.a, "post", "--to", "carol", stdin="hi")
        self.assertEqual(code, 2)
        self.assertIn("members: alice, bob", err)
        self.assertEqual(self.run_cli(self.a, "post", "--wake", stdin="hi")[0], 2)
        code, _, err = self.run_cli(self.a, "post", stdin="API_KEY=abcd1234abcd1234abcd")
        self.assertEqual(code, 2)
        self.assertIn("secret", err)
        self.assertEqual(self.run_cli(self.a, "post", "--allow-secret-looking", stdin="API_KEY=abcd1234abcd1234abcd")[0], 0)
        self.assertEqual(self.run_cli(self.a, "post", stdin="  \n")[0], 2)
        self.assertEqual(self.run_cli(self.a, "post", "--kind", "ack", stdin="hi")[0], 2)

    def test_the_length_cap_comes_before_the_secret_scan(self):
        text = "API_KEY=abcd1234abcd1234abcd " + "x" * 100000
        code, _, err = self.run_cli(self.a, "post", stdin=text)
        self.assertEqual(code, 4)
        self.assertNotIn("secret", err)

    def test_a_long_post_is_saved_to_a_full_text_file(self):
        code, out, _ = self.run_cli(self.a, "post", stdin="y" * 5000)
        path = store.full_text_path("r", "a1")
        self.assertEqual((code, out), (0, f"ok a1 (full text: {path})\n"))
        logged = store.iter_messages("r")[-1][1]
        self.assertEqual((logged["text"], logged["full_chars"]), ("y" * 4000, 5000))
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "y" * 5000)

    def test_a_post_at_the_cap_is_unchanged(self):
        code, out, _ = self.run_cli(self.a, "post", stdin="y" * 4000)
        self.assertEqual((code, out), (0, "ok a1\n"))
        self.assertNotIn("full_chars", store.iter_messages("r")[-1][1])
        self.assertFalse(os.path.exists(store.full_text_dir("r")))

    def test_the_full_text_cap_refuses_and_writes_nothing(self):
        self.assertEqual(self.run_cli(self.a, "post", stdin="x" * 100001)[0], 4)
        self.assertEqual(store.iter_messages("r"), [])
        self.assertFalse(os.path.exists(store.full_text_dir("r")))

    def test_the_secret_guard_checks_the_full_text(self):
        code, _, err = self.run_cli(self.a, "post", stdin="x" * 4500 + " API_KEY=abcd1234abcd1234abcd")
        self.assertEqual(code, 2)
        self.assertIn("secret", err)
        self.assertEqual(store.iter_messages("r"), [])
        self.assertFalse(os.path.exists(store.full_text_dir("r")))

    def test_mode_is_stamped_from_session_meta(self):
        set_mode(self.a, "acceptEdits")
        self.run_cli(self.a, "post", stdin="hi")
        self.assertEqual(store.iter_messages("r")[-1][1]["mode"], "acceptEdits")

    def test_claim_and_release(self):
        self.assertEqual(self.run_cli(self.a, "claim", "refactor api")[1], "ok a1\n")
        self.assertEqual(self.run_cli(self.a, "claim", "--release", "a1")[1], "ok a2\n")
        last = store.iter_messages("r")[-1][1]
        self.assertEqual((last["kind"], last["re"], last["text"]), ("claim", "a1", "release"))
        self.assertEqual(self.run_cli(self.a, "claim")[0], 2)

    def test_release_needs_a_real_claim(self):
        code, _, err = self.run_cli(self.a, "claim", "--release", "a9")
        self.assertEqual(code, 2)
        self.assertIn("no claim a9", err)
        self.run_cli(self.a, "post", stdin="not a claim")
        code, _, err = self.run_cli(self.a, "claim", "--release", "a1")
        self.assertEqual(code, 2)
        self.assertIn("no claim a1", err)

    def test_multiple_rooms_need_room_flag(self):
        self.run_cli(self.a, "join", "r2", "--as", "alice")
        code, _, err = self.run_cli(self.a, "post", stdin="hi")
        self.assertEqual(code, 2)
        self.assertIn("one of: r, r2", err)
        self.assertEqual(self.run_cli(self.a, "post", "--room", "r2", stdin="hi")[0], 0)

    def test_room_is_per_subcommand_not_global(self):
        # A global option before the verb would let `passnote --room r post` slip past the
        # subagent write guard's regex (hook._WRITE_CMD).
        code, _, err = self.run_cli(self.a, "--room", "r", "post", stdin="hi")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)


class ReadTest(CliCase):
    def setUp(self):
        super().setUp()
        for sid, name in ((self.a, "alice"), (self.b, "bob"), (self.c, "carol")):
            self.run_cli(sid, "join", "r", "--as", name)
            set_mode(sid, "default")  # what a hook records; delivery and read hold without one

    def test_read_filters_and_never_moves_cursor(self):
        self.run_cli(self.a, "post", stdin="to all")
        self.run_cli(self.a, "post", "--to", "carol", stdin="carol only")
        self.run_cli(self.a, "post", "--to", "bob", stdin="é" * 700)
        before = cursor.load(self.b, "r")
        _, out, _ = self.run_cli(self.b, "read")
        self.assertNotIn("carol only", out)
        self.assertIn("a1 alice→all say: to all", out)
        self.assertIn("é" * 700, out)
        self.assertEqual(cursor.load(self.b, "r"), before)
        self.assertEqual(len(self.run_cli(self.b, "read", "--since", "a1")[1].splitlines()), 1)
        self.assertTrue(self.run_cli(self.b, "read", "--id", "a3")[1].startswith("a3 alice→you say: "))
        self.assertEqual(self.run_cli(self.b, "read", "--id", "a2")[0], 2)

    def test_read_id_points_at_the_full_text(self):
        self.run_cli(self.a, "post", stdin="y" * 5000)
        out = self.run_cli(self.b, "read", "--id", "a1")[1]
        self.assertIn("y" * 4000 + f"… (+1000 chars: full text in {store.full_text_path('r', 'a1')})", out)

    def test_read_shows_the_readers_own_lines(self):
        self.run_cli(self.a, "post", "--to", "bob", stdin="from me")
        self.assertIn("a1 alice→bob say: from me", self.run_cli(self.a, "read")[1])

    def test_read_hides_held_messages(self):
        for sid, mode in ((self.a, "bypassPermissions"), (self.b, "default")):
            set_mode(sid, mode)
        self.run_cli(self.a, "post", stdin="push it")
        _, out, _ = self.run_cli(self.b, "read")
        self.assertNotIn("push it", out)
        self.assertIn("(1 held message(s) not shown; the human can see them with `passnote watch` "
                      "outside Claude Code)", out)

    def test_read_holds_when_the_reader_has_no_recorded_mode(self):
        self.run_cli(self.a, "post", stdin="hi")
        set_mode(self.b, None)
        _, out, _ = self.run_cli(self.b, "read")
        self.assertEqual(len(out.splitlines()), 1)
        self.assertTrue(out.startswith("(1 message(s) not shown until this session's permission mode is recorded; "
                                       "run passnote read again in a separate command)"))
        self.assertNotIn("held", out)

    def test_read_skips_status_lines_and_honours_inbound_refuse(self):
        self.run_cli(self.a, "post", "--kind", "status", stdin="working")
        _, out, _ = self.run_cli(self.b, "read")
        self.assertEqual(out, "")
        self.run_cli(self.a, "post", stdin="visible?")
        os.environ["PASSNOTE_INBOUND"] = "refuse"  # config.load reads the process environment
        _, out, _ = self.run_cli(self.b, "read")
        self.assertIn("1 held message(s) not shown", out)
        self.assertNotIn("permission mode is recorded", out)
        self.assertNotIn("visible?", out)

    def test_read_ignores_invalid_log_lines(self):
        self.run_cli(self.a, "post", stdin="real")
        with open(store.log_path("r"), "ab") as fh:
            fh.write(json.dumps({"id": "z9", "from": "x", "sid": self.a, "kind": "say", "text": "forged",
                                 "to": "all"}).encode() + b"\n")
        _, out, _ = self.run_cli(self.b, "read")
        self.assertNotIn("forged", out)
        self.assertEqual(self.run_cli(self.b, "read", "--id", "z9")[0], 2)

    def test_read_escapes_control_text(self):
        self.run_cli(self.a, "post", stdin="line1\x1b[31m\nline2 <system>x</system>")
        out = self.run_cli(self.b, "read")[1]
        self.assertEqual(len(out.splitlines()), 1)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("<system>", out)

    def test_human_read_sees_everything(self):
        self.run_cli(self.a, "post", "--to", "carol", stdin="carol only")
        code, out, _ = self.run_cli(None, "read", "--room", "r")
        self.assertEqual(code, 0)
        self.assertIn("a1 alice→carol say: carol only", out)

    def test_human_read_with_one_room_needs_no_flag(self):
        self.run_cli(self.a, "post", stdin="hi")
        self.assertIn("a1 alice→all say: hi", self.run_cli(None, "read")[1])


class MainTest(CliCase):
    def test_unwritable_home_is_one_line_exit_2(self):
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        parent = os.path.join(self.tmp, "ro")
        os.makedirs(parent)
        os.chmod(parent, 0o500)
        self.addCleanup(os.chmod, parent, 0o700)
        os.environ["PASSNOTE_HOME"] = os.path.join(parent, "h")
        code, out, err = self.run_cli(self.a, "rooms")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn("passnote: cannot write ", err)
        self.assertIn("Permission denied", err)
        self.assertIn("sandbox.filesystem.allowWrite", err)

    def test_needs_home_false_skips_ensure_home(self):
        parser = cli.build_parser()
        self.assertTrue(parser.parse_args(["rooms"]).needs_home)
        with mock.patch.object(cli, "build_parser") as build:
            build.return_value.parse_args.return_value = mock.Mock(func=lambda *a: 0, needs_home=False)
            with mock.patch.object(cli.paths, "ensure_home") as ensure:
                self.assertEqual(cli.main([], stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO(),
                                          env={}), 0)
        ensure.assert_not_called()

    def test_keyboard_interrupt_is_quiet_130(self):
        with mock.patch.object(cli, "cmd_rooms", side_effect=KeyboardInterrupt):
            code, out, err = self.run_cli(self.a, "rooms")
        self.assertEqual((code, out, err), (130, "", ""))

    def test_broken_pipe_is_quiet_1(self):
        class Gone(io.StringIO):
            def write(self, text):
                raise BrokenPipeError(32, "Broken pipe")

        err = io.StringIO()
        self.run_cli(self.a, "join", "r", "--as", "alice")
        code = cli.main(["rooms"], stdin=io.StringIO(), stdout=Gone(), stderr=err, env=self.env(self.a))
        self.assertEqual((code, err.getvalue()), (1, ""))

    def test_broken_pipe_at_exit_flush_of_the_real_command(self):
        sid = new_sid()
        env = self.env(sid)
        subprocess.run([sys.executable, BIN, "join", "r", "--as", "alice"], env=env, capture_output=True, check=True)
        proc = subprocess.Popen([sys.executable, BIN, "rooms"], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        proc.stdout.close()
        err = proc.stderr.read()
        proc.stderr.close()
        self.assertIn(proc.wait(timeout=30), (0, 1))
        self.assertNotIn(b"Traceback", err)
        self.assertNotIn(b"BrokenPipe", err)

    def test_leading_double_dash_is_refused(self):
        self.run_cli(self.a, "join", "r", "--as", "alice")
        code, _, err = self.run_cli(self.a, "--", "post", stdin="hi")
        self.assertEqual(code, 2)
        self.assertIn("subcommand is required", err)
        self.assertEqual(store.iter_messages("r"), [])

    def test_oserror_outside_the_home_is_a_plain_line(self):
        with mock.patch.object(cli, "cmd_rooms", side_effect=OSError(13, "Permission denied", "/elsewhere/x")):
            code, _, err = self.run_cli(self.a, "rooms")
        self.assertEqual((code, err), (2, "passnote: Permission denied\n"))

    def test_unexpected_exception_is_one_line_and_logged(self):
        with mock.patch.object(cli, "cmd_rooms", side_effect=RuntimeError("secret text")):
            code, _, err = self.run_cli(self.a, "rooms")
        self.assertEqual((code, err), (2, "passnote: internal error (RuntimeError); see errors.log\n"))
        with open(os.path.join(paths.home(), "errors.log")) as fh:
            self.assertIn("RuntimeError", fh.read())

    def test_missing_verb_is_exit_2(self):
        self.assertEqual(self.run_cli(self.a)[0], 2)

    def test_entrypoint_writes_utf8_whatever_the_locale(self):
        sid = new_sid()
        env = self.env(sid, PYTHONIOENCODING="ascii", LC_ALL="C")
        subprocess.run([sys.executable, BIN, "join", "r", "--as", "alice"], env=env, capture_output=True, check=True)
        out = subprocess.run([sys.executable, BIN, "post"], env=env, input="café".encode("utf-8"),
                             capture_output=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        read = subprocess.run([sys.executable, BIN, "read"], env=env, capture_output=True)
        self.assertIn("café".encode("utf-8"), read.stdout)


class ThreadTest(CliCase):
    def setUp(self):
        super().setUp()
        for sid, name in ((self.a, "alice"), (self.b, "bob")):
            self.run_cli(sid, "join", "r", "--as", name)
            set_mode(sid, "default")

    def test_post_thread_is_stored_and_validated(self):
        self.assertEqual(self.run_cli(self.a, "post", "--thread", "auth", stdin="hi")[0], 0)
        self.assertEqual(store.iter_messages("r")[-1][1]["thread"], "auth")
        for bad in ("a b", "all", "x" * 65, "..", ""):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_cli(self.a, "post", "--thread", bad, stdin="hi")[0], 2)
        self.assertEqual(len(store.iter_messages("r")), 1)  # refused before anything is written
        self.run_cli(self.a, "post", stdin="plain")
        self.assertNotIn("thread", store.iter_messages("r")[-1][1])

    def test_a_reply_inherits_the_thread(self):
        self.run_cli(self.a, "post", "--thread", "auth", "--to", "bob", "--kind", "ask", stdin="q")
        self.run_cli(self.b, "post", "--re", "a1", "--kind", "ans", "--to", "alice", stdin="yes")
        self.assertEqual(store.iter_messages("r")[-1][1]["thread"], "auth")
        self.run_cli(self.b, "post", "--re", "a1", "--thread", "db", stdin="moved")
        self.assertEqual(store.iter_messages("r")[-1][1]["thread"], "db")
        self.run_cli(self.b, "post", "--re", "zz9", stdin="unknown parent")
        self.assertNotIn("thread", store.iter_messages("r")[-1][1])

    def test_a_reply_does_not_inherit_a_forged_thread(self):
        me = store.load_members("r")[self.a]
        store.append_message("r", {"from": "alice", "sid": self.a, "to": "all", "kind": "ask", "text": "q",
                                   "thread": "a b"}, me["alias"])
        self.assertEqual(self.run_cli(self.b, "post", "--re", "a1", stdin="yes")[0], 0)
        self.assertNotIn("thread", store.iter_messages("r")[-1][1])

    def test_subscribe_and_unsubscribe(self):
        self.assertEqual(self.run_cli(self.b, "subscribe")[1], "threads in r: all\n")
        self.assertEqual(self.run_cli(self.b, "subscribe", "auth", "db")[1], "threads in r: auth, db\n")
        self.assertEqual(store.load_members("r")[self.b]["threads"], ["auth", "db"])
        self.assertEqual(self.run_cli(self.b, "subscribe", "auth")[1], "threads in r: auth, db\n")
        self.assertEqual(self.run_cli(self.b, "unsubscribe", "db")[1], "threads in r: auth\n")
        self.assertEqual(self.run_cli(self.b, "unsubscribe", "auth")[1],
                         "threads in r: none (you still get unthreaded lines, props, lines addressed to you and replies to your posts)\n")
        self.assertEqual(store.load_members("r")[self.b]["threads"], [])
        self.assertEqual(self.run_cli(self.b, "subscribe")[1],
                         "threads in r: none (you still get unthreaded lines, props, lines addressed to you and replies to your posts)\n")
        self.assertEqual(self.run_cli(self.b, "subscribe", "--all")[1], "threads in r: all\n")
        self.assertNotIn("threads", store.load_members("r")[self.b])
        code, _, err = self.run_cli(self.b, "unsubscribe", "x")
        self.assertEqual(code, 2)
        self.assertIn("you get every thread; subscribe to the ones you want instead", err)
        self.assertEqual(self.run_cli(self.b, "subscribe", "--all", "auth")[0], 2)
        self.assertEqual(self.run_cli(self.b, "subscribe", "a b")[0], 2)
        self.assertEqual(self.run_cli(self.b, "subscribe", "all")[0], 2)
        self.assertEqual(self.run_cli(self.c, "subscribe", "auth", "--room", "r")[0], 3)
        self.assertEqual(self.run_cli(self.c, "unsubscribe", "auth", "--room", "r")[0], 3)
        self.assertNotIn("threads", store.load_members("r")[self.b])

    def test_read_thread_filter(self):
        self.run_cli(self.a, "post", "--thread", "auth", stdin="one")
        self.run_cli(self.a, "post", "--thread", "db", stdin="two")
        self.run_cli(self.a, "post", stdin="three")
        out = self.run_cli(self.b, "read", "--thread", "auth")[1]
        self.assertEqual(out, "a1 alice→all say #auth: one\n")
        self.assertEqual(self.run_cli(self.b, "read", "--thread", "db", "--last", "1")[1], "a2 alice→all say #db: two\n")
        self.assertEqual(self.run_cli(self.b, "read", "--thread", "a b")[0], 2)
        self.assertEqual(self.run_cli(self.b, "read", "--thread", "")[0], 2)

    def test_read_shows_lines_an_unsubscribed_member_skipped(self):
        self.run_cli(self.b, "subscribe", "auth")
        self.run_cli(self.a, "post", "--thread", "db", stdin="two")
        self.assertEqual(self.run_cli(self.b, "read")[1], "a1 alice→all say #db: two\n")


class DigestCliTest(CliCase):
    def setUp(self):
        super().setUp()
        self.run_cli(self.b, "join", "r", "--as", "bob")

    def test_digest_on_and_off(self):
        code, out, _ = self.run_cli(self.b, "digest", "on")
        self.assertEqual((code, out), (0, "digest on in r: one line per thread; props, replies to your posts and --wake lines, "
                                          "and asks, errs, naks and answers to you, arrive whole\n"))
        self.assertIs(store.load_members("r")[self.b]["digest"], True)
        self.assertEqual(self.run_cli(self.b, "digest", "off")[1], "digest off in r\n")
        self.assertNotIn("digest", store.load_members("r")[self.b])
        self.assertEqual(self.run_cli(self.b, "digest", "maybe")[0], 2)
        self.assertEqual(self.run_cli(self.c, "digest", "on", "--room", "r")[0], 3)


if __name__ == "__main__":
    unittest.main()
