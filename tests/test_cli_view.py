import io
import json
import os
import stat
import subprocess
import time
import unittest
from unittest import mock

from support import CliCase
from passnote import cli, cursor, paths, sessions, store


def set_pid(sid, pid):
    sessions.update_meta(sid, lambda meta: meta.update(pid=pid))


class WhoTest(CliCase):
    def setUp(self):
        super().setUp()
        for sid, name in ((self.a, "alice"), (self.b, "bob"), (self.c, "carol")):
            self.run_cli(sid, "join", "r", "--as", name)

    def test_who_shows_members_unanswered_claims_status_and_props(self):
        sessions.touch_active(self.a)
        sessions.touch_active(self.b, time.time() - 7200)
        for sid, mode in ((self.a, "default"), (self.b, "bypassPermissions")):
            sessions.update_meta(sid, lambda meta, mode=mode: meta.update(permission_mode=mode))
        self.run_cli(self.a, "post", "--to", "bob", "--kind", "ask", stdin="review pr 12?")
        self.run_cli(self.a, "claim", "refactor api")
        self.run_cli(self.b, "post", "--kind", "status", stdin="running tests")
        self.run_cli(self.a, "post", "--to", "carol", "--kind", "prop", stdin="ship at 3pm")
        code, out, _ = self.run_cli(self.a, "who")
        self.assertEqual(code, 0)
        self.assertIn("alice (a) · warm · mode default · you", out)
        self.assertIn("bob (b) · cold (last active 120m ago) · mode bypassPermissions", out)
        self.assertIn("carol (c) · cold (never active)", out)
        self.assertNotIn("idle", out)
        self.assertIn("note: different permission class", out)
        # doctor's wording; post holds every doorbell between classes (wake.held)
        self.assertIn("held (only the human sees them) unless the receiver was launched with "
                      "PASSNOTE_ALLOW_BYPASS=1; post never rings a doorbell between classes", out)
        self.assertIn("unanswered a1 ask from alice → waiting on bob: review pr 12?", out)
        self.assertNotIn("pending", out)
        self.assertIn("claim a2 alice: refactor api", out)
        # bob is in another permission class: his status is held from alice's session, not from the human
        self.assertNotIn("running tests", out)
        self.assertIn("(1 message(s) from sessions in a different permission class not shown", out)
        self.assertIn("status bob: running tests", self.run_cli(None, "who", "--room", "r")[1])
        self.assertIn("prop a4 from alice · seen by nobody · not yet seen by carol", out)

    def test_human_who_needs_no_session(self):
        code, out, _ = self.run_cli(None, "who", "--room", "r")
        self.assertEqual(code, 0)
        self.assertIn("carol (c)", out)
        self.assertNotIn("note: different permission class", out)

    def test_who_notes_no_class_mismatch_before_this_sessions_mode_is_recorded(self):
        sessions.update_meta(self.a, lambda meta: meta.update(permission_mode="default"))
        sessions.update_meta(self.b, lambda meta: meta.update(permission_mode="bypassPermissions"))
        _, out, _ = self.run_cli(self.c, "who")  # carol's mode is not recorded yet
        self.assertNotIn("note: different permission class", out)

    def test_who_in_an_empty_home_creates_nothing(self):
        os.environ["PASSNOTE_HOME"] = os.path.join(self.tmp, "fresh")
        code, _, err = self.run_cli(None, "who")
        self.assertEqual(code, 2)
        self.assertIn("pass --room", err)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "fresh")))

    def test_who_marks_a_member_whose_process_is_gone(self):
        set_pid(self.b, 2 ** 31 - 2)  # no such process
        sessions.touch_active(self.b, time.time() - 7200)
        _, out, _ = self.run_cli(None, "who", "--room", "r")
        self.assertIn("bob (b) · cold (last active 120m ago) · gone", out)
        self.assertNotIn("alice (a) · cold (never active) · gone", out)  # unknown pid is not gone
        set_pid(self.c, os.getpid())
        sessions.update_meta(self.c, lambda meta: meta.update(pid_started_at=sessions.pid_started_at(os.getpid())))
        self.assertNotIn("carol (c) · cold (never active) · gone", self.run_cli(None, "who", "--room", "r")[1])

    def test_a_held_prop_is_not_seen(self):
        self.run_cli(self.a, "post", "--to", "bob,carol", "--kind", "prop", stdin="ship at 3pm")
        for sid in (self.b, self.c):  # both cursors are past it
            paths.atomic_write_json(cursor.path(sid, "r"), {"ino": 1, "off": 1, "seq": 1})
        store.append_event("r", {"type": "hold", "reason": "mode", "id": "a1", "to_sid": self.c})
        _, out, _ = self.run_cli(None, "who", "--room", "r")
        self.assertIn("prop a1 from alice · seen by bob · not yet seen by carol", out)

    def _different_class_activity(self):
        """alice is default; bob (bypassPermissions) asks alice, claims work and posts a status."""
        for sid, mode in ((self.a, "default"), (self.b, "bypassPermissions"), (self.c, "default")):
            sessions.update_meta(sid, lambda meta, mode=mode: meta.update(permission_mode=mode))
        self.run_cli(self.b, "post", "--to", "alice", "--kind", "ask", stdin="secret plan?")
        self.run_cli(self.b, "claim", "bob's hidden work")
        self.run_cli(self.b, "post", "--kind", "status", stdin="bob status text")
        self.run_cli(self.a, "post", "--to", "bob", "--kind", "ask", stdin="alice asks bob")
        self.run_cli(self.a, "claim", "alice work")

    def test_who_in_session_hides_a_different_class_senders_gists(self):
        self._different_class_activity()
        code, out, _ = self.run_cli(self.a, "who")
        self.assertEqual(code, 0)
        for text in ("secret plan?", "bob's hidden work", "bob status text"):
            self.assertNotIn(text, out)
        self.assertIn("unanswered a4 ask from alice → waiting on bob: alice asks bob", out)  # own kept
        self.assertIn("claim a5 alice: alice work", out)
        self.assertIn("(3 message(s) from sessions in a different permission class not shown; the human can see "
                      "them with passnote watch outside Claude Code)", out)
        _, carol_out, _ = self.run_cli(self.c, "who")  # held regardless of addressing
        self.assertNotIn("bob status text", carol_out)
        self.assertNotIn("secret plan?", carol_out)

    def test_human_who_shows_every_senders_gists(self):
        self._different_class_activity()
        _, out, _ = self.run_cli(None, "who", "--room", "r")
        for text in ("secret plan?", "bob's hidden work", "bob status text", "alice asks bob"):
            self.assertIn(text, out)
        self.assertNotIn("not shown", out)

    def test_who_hides_others_until_this_sessions_mode_is_recorded(self):
        self.run_cli(self.b, "post", "--kind", "status", stdin="bob status text")
        self.run_cli(self.a, "post", "--kind", "status", stdin="alice status text")
        _, out, _ = self.run_cli(self.a, "who")  # alice's mode is not recorded yet
        self.assertNotIn("bob status text", out)
        self.assertIn("alice status text", out)
        self.assertIn("(1 message(s) not shown until this session's permission mode is recorded; "
                      "run passnote who again in a separate command)", out)

    def test_who_skips_malformed_log_lines_and_escapes_text(self):
        with open(store.log_path("r"), "ab") as fh:
            fh.write(b'{"seq": "x", "id": "z1", "from": "alice", "sid": "s", "kind": "ask", "to": ["bob"], "text": "bad"}\n')
            fh.write(b"not json\n")
        self.run_cli(self.a, "post", "--to", "bob", "--kind", "ask", stdin="x\n\x1b[31mred\x1b[0m " + "y" * 200)
        _, out, _ = self.run_cli(None, "who", "--room", "r")
        self.assertNotIn("z1", out)
        self.assertNotIn("\x1b", out)
        line = [row for row in out.splitlines() if row.startswith("unanswered")][0]
        self.assertIn("x\\nred ", line)
        self.assertLessEqual(len(line.split(": ", 1)[1]), 80)


    def test_who_shows_threads(self):
        self.run_cli(self.b, "subscribe", "auth")
        out = self.run_cli(self.a, "who")[1]
        self.assertRegex(out, r"bob \(b\) · [^\n]* · threads auth\n")
        self.assertNotIn("threads", [line for line in out.split("\n") if line.startswith("  alice")][0])
        self.run_cli(self.b, "unsubscribe", "auth")
        self.assertRegex(self.run_cli(self.b, "who")[1], r"bob \(b\) · [^\n]* · threads none · you\n")


class WatchTest(CliCase):
    def setUp(self):
        super().setUp()
        self.run_cli(self.a, "join", "r", "--as", "alice")
        self.run_cli(self.b, "join", "r", "--as", "bob")

    def follow(self, steps, *argv, out=None):
        """Run `watch` until the scripted steps (one per sleep) run out, then Ctrl-C."""
        queue = list(steps)

        def sleep(_):
            if not queue:
                raise KeyboardInterrupt
            queue.pop(0)()

        out, err = out or io.StringIO(), io.StringIO()
        with mock.patch("passnote.cli.time.sleep", sleep):
            code = cli.main(["watch", *argv], stdin=io.StringIO(), stdout=out, stderr=err, env=dict(os.environ))
        return code, out.getvalue(), err.getvalue()

    def test_watch_once_prints_recent_messages_and_events(self):
        for sid in (self.a, self.b):  # same class: bob could be woken, were he warm
            sessions.update_meta(sid, lambda meta: meta.update(permission_mode="default"))
        self.run_cli(self.a, "post", "--to", "bob", "--kind", "ask", stdin="line1\nline2")
        code, out, _ = self.run_cli(None, "watch", "r", "--once")
        self.assertEqual(code, 0)
        self.assertIn("[r] a1 alice→bob ask: line1\\nline2", out)
        self.assertIn("[r] · join bob", out)
        self.assertIn("[r] · wake WAIT bob a1 (cold (never active; --urgent to force))", out)
        self.assertNotIn("QUEUED", out)
        code, out, _ = self.run_cli(None, "watch", "--all", "--once")
        self.assertIn("a1 alice", out)

    def test_watch_shows_the_thread(self):
        self.run_cli(self.a, "post", "--thread", "auth", stdin="token rotated")
        self.assertIn("[r] a1 alice→all say #auth: token rotated", self.run_cli(None, "watch", "r", "--once")[1])

    def test_watch_points_a_long_post_at_its_full_text_file(self):
        self.run_cli(self.a, "post", stdin="y" * 5000)
        note = f"… (+1000 chars: full text in {store.full_text_path('r', 'a1')})"
        self.assertIn("y" * 4000 + note, self.run_cli(None, "watch", "r", "--once")[1])
        _, out, _ = self.follow([lambda: self.run_cli(self.a, "post", stdin="z" * 4500)], "r", "--last", "0")
        self.assertIn("z" * 4000 + f"… (+500 chars: full text in {store.full_text_path('r', 'a2')})", out)

    def test_watch_shows_unanswered_once_at_startup_before_the_recent_lines(self):
        self.run_cli(self.a, "post", "--to", "bob", "--kind", "ask", stdin="review pr 12?")
        _, out, _ = self.run_cli(None, "watch", "r", "--once")
        lines = out.splitlines()
        self.assertEqual(lines[0], "[r] unanswered a1 ask from alice → waiting on bob: review pr 12?")
        self.assertEqual(sum("unanswered" in line for line in lines), 1)
        self.run_cli(self.b, "post", "--to", "alice", "--kind", "ans", "--re", "a1", stdin="lgtm")
        self.assertNotIn("unanswered", self.run_cli(None, "watch", "r", "--once")[1])

    def test_watch_renders_hold_events_with_the_addressee_name(self):
        self.run_cli(self.a, "post", "--to", "bob", "--kind", "say", stdin="hi")
        store.append_event("r", {"type": "hold", "reason": "mode", "id": "a1", "to_sid": self.b})
        store.append_event("r", {"type": "hold", "reason": "mode", "id": "a1", "to_sid": "ffffffffffff"})
        _, out, _ = self.run_cli(None, "watch", "r", "--once")
        self.assertIn("[r] · hold a1 → bob (mode)", out)
        self.assertIn("[r] · hold a1 → ffffffff (mode)", out)

    def test_watch_refuses_to_run_inside_a_claude_session(self):
        self.run_cli(self.a, "post", "--to", "bob", "--kind", "ask", stdin="held from the model")
        for argv in (("watch", "r", "--once"), ("watch", "--all", "--once")):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(self.b, *argv)
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(err, "passnote: watch shows held messages, so it runs only in a terminal "
                                      "outside Claude Code (see passnote shim)\n")

    def test_unknown_room_is_an_error_and_no_rooms_is_one_line(self):
        for argv in (("who", "--room", "nosuch"), ("watch", "nosuch", "--once")):
            code, out, err = self.run_cli(None, *argv)
            self.assertEqual((code, out), (2, ""))
            self.assertIn("no such room: nosuch", err)
        os.environ["PASSNOTE_HOME"] = os.path.join(self.tmp, "fresh")
        code, out, _ = self.run_cli(None, "watch", "--all", "--once")
        self.assertEqual((code, out), (0, "(no rooms yet)\n"))

    def test_an_unterminated_line_larger_than_the_window_does_not_crash(self):
        record = {"seq": 2, "id": "a2", "from": "alice", "sid": self.a, "kind": "say", "to": "all",
                  "text": "x" * 300, "ts": time.time()}
        event = {"type": "join", "name": "n" * 150, "ts": time.time()}
        for path, rec, shown in ((store.log_path("r"), record, "a2 alice"), (store.events_path("r"), event, "· join nnn")):
            with self.subTest(path=os.path.basename(path)):
                self.run_cli(self.a, "post", "--kind", "say", stdin="earlier")
                line = json.dumps(rec)
                with open(path, "ab") as fh:
                    fh.write(line[:200].encode())  # no newline, longer than the (patched) window
                with mock.patch.object(store, "MAX_READ", 100):
                    code, out, err = self.run_cli(None, "watch", "r", "--once")
                    self.assertEqual((code, err), (0, ""), err)
                    out = io.StringIO()

                    def finish():
                        with open(path, "ab") as fh:
                            fh.write(line[200:].encode() + b"\n")

                    self.follow([lambda: None, finish, lambda: None], "r", out=out)
                self.assertEqual(out.getvalue().count(shown), 1)

    def test_watch_skips_invalid_lines_and_escapes(self):
        with open(store.log_path("r"), "ab") as fh:
            fh.write(b'{"seq": 0, "id": "z1", "from": "alice", "sid": "s", "kind": "say", "to": "all", "text": "bad"}\n')
        self.run_cli(self.a, "post", "--kind", "say", stdin="\x1b[31mred")
        _, out, _ = self.run_cli(None, "watch", "r", "--once")
        self.assertNotIn("z1", out)
        self.assertNotIn("\x1b", out)

    def test_follow_prints_appended_lines_once_and_ctrl_c_is_quiet(self):
        self.run_cli(self.a, "post", "--kind", "say", stdin="first")
        code, out, err = self.follow(
            [lambda: self.run_cli(self.a, "post", "--kind", "say", stdin="second"), lambda: None],
            "r")
        self.assertEqual((code, err), (130, ""))
        self.assertEqual(out.count("first"), 1)
        self.assertEqual(out.count("second"), 1)

    def test_follow_waits_for_the_end_of_a_line(self):
        log = store.log_path("r")
        line = json.dumps({"seq": 1, "id": "a1", "from": "alice", "sid": self.a, "kind": "say", "to": "all",
                           "text": "whole", "ts": time.time()})

        def start():
            with open(log, "ab") as fh:
                fh.write(line[:20].encode())

        def finish():
            with open(log, "ab") as fh:
                fh.write(line[20:].encode() + b"\n")

        seen, out = [], io.StringIO()
        self.follow([start, lambda: seen.append("whole" in out.getvalue()), finish, lambda: None], "r", out=out)
        self.assertEqual(seen, [False])
        self.assertEqual(out.getvalue().count("whole"), 1)

    def test_follow_restarts_from_the_top_of_a_replaced_log(self):
        self.run_cli(self.a, "post", "--kind", "say", stdin="old one")
        self.run_cli(self.a, "post", "--kind", "say", stdin="old two")
        log = store.log_path("r")

        def replace():
            new = log + ".new"
            with open(new, "w") as fh:  # shorter than the old log, so size < offset too
                fh.write(json.dumps({"seq": 1, "id": "a1", "from": "alice", "sid": self.a, "kind": "say",
                                     "to": "all", "text": "new one", "ts": time.time()}) + "\n")
            os.replace(new, log)

        _, out, _ = self.follow([replace, lambda: None], "r")
        self.assertEqual(out.count("old one"), 1)
        self.assertEqual(out.count("new one"), 1)

    def test_follow_restarts_when_the_log_is_replaced_by_a_longer_one(self):
        self.run_cli(self.a, "post", "--kind", "say", stdin="old")
        log = store.log_path("r")

        def replace():
            new = log + ".new"
            with open(new, "w") as fh:
                for seq in (1, 2, 3):
                    fh.write(json.dumps({"seq": seq, "id": f"a{seq}", "from": "alice", "sid": self.a,
                                         "kind": "say", "to": "all", "text": f"fresh {seq}"}) + "\n")
            os.replace(new, log)

        _, out, _ = self.follow([replace, lambda: None], "r")
        for seq in (1, 2, 3):
            self.assertEqual(out.count(f"fresh {seq}"), 1)

    def test_follow_shows_new_events(self):
        _, out, _ = self.follow([lambda: self.run_cli(self.c, "join", "r", "--as", "carol"), lambda: None], "r")
        self.assertIn("· join carol", out)

    def test_color_only_for_a_terminal_without_no_color_or_dumb_term(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True

        self.run_cli(self.a, "post", "--to", "bob", "--kind", "ask", stdin="q")

        def run(**env_extra):
            env = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "TERM")}
            env.update(env_extra)
            out = Tty()
            cli.main(["watch", "r", "--once"], stdin=io.StringIO(), stdout=out, stderr=io.StringIO(), env=env)
            return out.getvalue()

        self.assertIn("\x1b[", run(TERM="xterm-256color"))
        self.assertIn("\x1b[", run(TERM="xterm-256color", NO_COLOR=""))
        self.assertNotIn("\x1b[", run(TERM="xterm-256color", NO_COLOR="1"))
        self.assertNotIn("\x1b[", run(TERM="dumb"))
        self.assertNotIn("\x1b[", self.run_cli(None, "watch", "r", "--once")[1])  # not a tty

    def test_brand_palette_with_24_bit_color_else_basic_colors(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True

        self.run_cli(self.a, "post", "--to", "bob", "--kind", "ask", stdin="review pr 12?")
        self.run_cli(self.a, "post", "--kind", "prop", stdin="ship it")
        store.append_event("r", {"type": "wake", "decision": "WAKE", "reason": "warm", "id": "a1",
                                 "to_sid": self.b, "to": "bob"})

        def run(**env_extra):
            env = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "TERM", "COLORTERM")}
            env.update(TERM="xterm-256color", **env_extra)
            out = Tty()
            cli.main(["watch", "r", "--once"], stdin=io.StringIO(), stdout=out, stderr=io.StringIO(), env=env)
            return out.getvalue().splitlines()

        def line(lines, needle):  # the log line, not the uncoloured "unanswered" summary
            return next(text for text in lines if needle in text and "unanswered" not in text)

        for value in ("truecolor", "24bit"):
            lines = run(COLORTERM=value)
            self.assertTrue(line(lines, "review pr 12?").startswith("\x1b[38;2;232;52;78m"))  # Margin
            self.assertTrue(line(lines, "ship it").startswith("\x1b[38;2;61;91;217m"))  # Ruled
            self.assertTrue(line(lines, "wake WAKE bob").startswith(
                "\x1b[38;2;27;23;20;48;2;255;225;77m"))  # Ink on Highlighter: the doorbell
            self.assertTrue(line(lines, "· join").startswith("\x1b[38;2;110;104;98m"))  # Graphite
        lines = run()
        self.assertTrue(line(lines, "review pr 12?").startswith("\x1b[31m"))
        self.assertTrue(line(lines, "ship it").startswith("\x1b[36m"))
        self.assertTrue(line(lines, "wake WAKE bob").startswith("\x1b[30;43m"))
        self.assertTrue(line(lines, "· join").startswith("\x1b[2m"))


class UnsafeHomeTest(CliCase):
    def test_who_watch_and_uninstall_refuse_a_home_others_can_write_to(self):
        self.run_cli(self.a, "join", "r", "--as", "alice")
        os.chmod(self.home, 0o777)
        for argv in (("who", "--room", "r"), ("watch", "r", "--once"), ("uninstall", "--purge", "--yes")):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(None, *argv)
                self.assertEqual(code, 2)
                self.assertIn("writable by group or others", err)
                self.assertEqual(out, "")
        self.assertTrue(os.path.isdir(os.path.join(self.home, "rooms")))

    def test_a_missing_home_is_not_created_by_the_check(self):
        os.environ["PASSNOTE_HOME"] = os.path.join(self.tmp, "fresh")
        self.run_cli(None, "watch", "--all", "--once")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "fresh")))


class GcTest(CliCase):
    def test_gc(self):
        self.run_cli(self.a, "join", "r", "--as", "alice")
        sessions.touch_active(self.a, time.time() - 30 * 86400)
        code, out, _ = self.run_cli(None, "gc", "--days", "7")
        self.assertEqual(code, 0)
        self.assertIn("removed 1 stale session(s)", out)


class UninstallTest(CliCase):
    def test_requires_purge_and_confirmation(self):
        self.run_cli(self.a, "join", "r", "--as", "alice")
        self.assertEqual(self.run_cli(None, "uninstall")[0], 2)
        self.assertEqual(self.run_cli(None, "uninstall", "--purge", stdin="no\n")[0], 2)
        self.assertEqual(self.run_cli(None, "uninstall", "--purge", stdin="")[0], 2)
        self.assertTrue(os.path.isdir(os.path.join(self.home, "rooms")))
        code, out, _ = self.run_cli(None, "uninstall", "--purge", stdin="purge\n")
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.home, "rooms")))
        self.assertFalse(os.path.exists(self.home))  # nothing else was in it
        self.assertIn("/plugin uninstall passnote", out)

    def test_yes_skips_the_prompt(self):
        self.run_cli(self.a, "join", "r", "--as", "alice")
        self.assertEqual(self.run_cli(None, "uninstall", "--purge", "--yes")[0], 0)
        self.assertFalse(os.path.exists(self.home))

    def test_only_passnotes_own_children_are_deleted(self):
        user_home = os.path.join(self.tmp, "user-home")  # PASSNOTE_HOME=~
        os.makedirs(user_home, mode=0o700)
        os.environ["PASSNOTE_HOME"] = user_home
        self.run_cli(self.a, "join", "r", "--as", "alice")
        for name, body in (("notes.txt", "mine"), ("config.json", "{}"), ("errors.log", "x")):
            with open(os.path.join(user_home, name), "w") as fh:
                fh.write(body)
        os.mkdir(os.path.join(user_home, "documents"))
        code, _, _ = self.run_cli(None, "uninstall", "--purge", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(os.listdir(user_home)), ["documents", "notes.txt"])

    def test_a_symlinked_child_is_unlinked_not_followed(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "keep"), "w") as fh:
            fh.write("x")
        os.makedirs(self.home, mode=0o700)
        os.symlink(outside, os.path.join(self.home, "rooms"))
        self.assertEqual(self.run_cli(None, "uninstall", "--purge", "--yes")[0], 0)
        self.assertTrue(os.path.exists(os.path.join(outside, "keep")))
        self.assertFalse(os.path.lexists(os.path.join(self.home, "rooms")))

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_failures_are_reported_and_exit_2(self):
        self.run_cli(self.a, "join", "r", "--as", "alice")
        stuck = os.path.join(self.home, "rooms", "r")
        os.chmod(stuck, 0o500)
        self.addCleanup(os.chmod, stuck, 0o700)
        code, _, err = self.run_cli(None, "uninstall", "--purge", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("rooms", err)
        self.assertEqual(err.count("\n"), 1)  # the details are in the one error line
        self.assertFalse(os.path.exists(os.path.join(self.home, "sessions")))  # the rest still went
        self.assertTrue(os.path.isdir(self.home))

    def test_nothing_to_delete(self):
        os.environ["PASSNOTE_HOME"] = os.path.join(self.tmp, "fresh")
        code, out, _ = self.run_cli(None, "uninstall", "--purge", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("nothing to delete", out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "fresh")))


class ShimTest(CliCase):
    def plugin(self, version, orphaned=False, marketplace="mkt"):
        root = os.path.join(self.claude_home, "plugins", "cache", marketplace, "passnote", version)
        os.makedirs(os.path.join(root, "bin"))
        script = os.path.join(root, "bin", "passnote")
        with open(script, "w") as fh:
            fh.write('#!/bin/sh\necho "fake %s $*"\n' % version)
        os.chmod(script, 0o755)
        if orphaned:
            with open(os.path.join(root, ".orphaned_at"), "w") as fh:
                fh.write("1")
        return root

    def installed(self, document):
        plugins = os.path.join(self.claude_home, "plugins")
        os.makedirs(plugins, exist_ok=True)
        with open(os.path.join(plugins, "installed_plugins.json"), "w") as fh:
            if isinstance(document, str):
                fh.write(document)
            else:
                json.dump(document, fh)

    def shim(self):
        target = os.path.join(self.tmp, "bin", "passnote")
        code, _, err = self.run_cli(None, "shim", "--path", target)
        self.assertEqual(code, 0, err)
        return target

    def run_shim(self, target, *argv):
        return subprocess.run([target, *argv], env=dict(os.environ), capture_output=True, text=True)

    def test_shim_finds_installed_plugin(self):
        self.plugin("0.1.0")
        target = self.shim()
        self.assertTrue(os.stat(target).st_mode & stat.S_IXUSR)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o755)
        res = self.run_shim(target, "who", "--room", "r")
        self.assertEqual(res.stdout.strip(), "fake 0.1.0 who --room r")

    def test_newest_version_by_number_not_by_string(self):
        self.plugin("0.9.2")
        self.plugin("0.10.0")
        self.plugin("nightly")
        self.assertEqual(self.run_shim(self.shim(), "x").stdout.strip(), "fake 0.10.0 x")

    def test_an_orphaned_version_is_skipped(self):
        self.plugin("0.9.2")
        self.plugin("0.10.0", orphaned=True)
        self.assertEqual(self.run_shim(self.shim(), "x").stdout.strip(), "fake 0.9.2 x")

    def test_installed_plugins_json_wins_over_the_newest_directory(self):
        old = self.plugin("0.9.2")
        self.plugin("0.10.0")
        self.installed({"version": 2, "plugins": {
            "other@mkt": [{"installPath": "/nonexistent"}],
            "passnote@mkt": [{"scope": "user", "installPath": old, "version": "0.9.2"}]}})
        self.assertEqual(self.run_shim(self.shim(), "x").stdout.strip(), "fake 0.9.2 x")

    def test_installed_plugins_json_is_read_defensively(self):
        self.plugin("0.9.2")
        self.plugin("0.10.0")
        gone = os.path.join(self.tmp, "no-such-install")
        for document in ("not json", "[]", "null", '{"plugins": 3}', '{"plugins": {"passnote@mkt": "x"}}',
                         {"plugins": {"passnote@mkt": [{"installPath": gone}, 5, None]}},
                         {"plugins": {"passnote@mkt": {"installPath": 7}}}):
            with self.subTest(document=document):
                self.installed(document)
                self.assertEqual(self.run_shim(self.shim(), "x").stdout.strip(), "fake 0.10.0 x")

    def test_installed_plugins_json_entry_matched_by_name_and_flat_shape(self):
        old = self.plugin("0.9.2")
        self.plugin("0.10.0")
        self.installed({"plugins": [{"name": "passnote", "installPath": old}]})
        self.assertEqual(self.run_shim(self.shim(), "x").stdout.strip(), "fake 0.9.2 x")
        self.installed({"passnote@mkt": {"installPath": old}})
        self.assertEqual(self.run_shim(self.shim(), "x").stdout.strip(), "fake 0.9.2 x")

    def test_only_the_passnote_entry_matches(self):
        self.plugin("0.9.2")
        other = self.plugin("0.10.0")
        for key in ("passnote-dev@mkt", "passnotes@mkt", "xpassnote@mkt"):
            with self.subTest(key=key):
                self.installed({"plugins": {key: [{"installPath": other}]}})
                self.assertEqual(self.run_shim(self.shim(), "x").stdout.strip(), "fake 0.10.0 x")
        old = os.path.join(self.claude_home, "plugins", "cache", "mkt", "passnote", "0.9.2")
        self.installed({"plugins": {"passnote-dev@mkt": [{"installPath": other}], "passnote@mkt": [{"installPath": old}]}})
        self.assertEqual(self.run_shim(self.shim(), "x").stdout.strip(), "fake 0.9.2 x")
        self.installed({"plugins": [{"name": "passnote-dev", "installPath": other}, {"name": "passnote", "installPath": old}]})
        self.assertEqual(self.run_shim(self.shim(), "x").stdout.strip(), "fake 0.9.2 x")

    def test_an_entry_point_that_cannot_be_executed_is_one_line_exit_2(self):
        root = self.plugin("0.1.0")
        with open(os.path.join(root, "bin", "passnote"), "w") as fh:
            fh.write("#!/nonexistent/interpreter\n")
        res = self.run_shim(self.shim(), "x")
        self.assertEqual(res.returncode, 2)
        self.assertIn("passnote: cannot run", res.stderr)
        self.assertNotIn("Traceback", res.stderr)
        self.assertEqual(res.stderr.count("\n"), 1)

    def test_no_plugin_installed(self):
        res = self.run_shim(self.shim(), "x")
        self.assertEqual(res.returncode, 2)
        self.assertIn("plugin not found", res.stderr)

    def test_install_refuses_to_overwrite_a_foreign_file_unless_forced(self):
        target = os.path.join(self.tmp, "bin", "passnote")
        os.makedirs(os.path.dirname(target))
        with open(target, "w") as fh:
            fh.write("#!/bin/sh\necho mine\n")
        code, _, err = self.run_cli(None, "shim", "--path", target)
        self.assertEqual(code, 2)
        self.assertIn("--force", err)
        with open(target) as fh:
            self.assertIn("echo mine", fh.read())
        self.assertEqual(self.run_cli(None, "shim", "--path", target, "--force")[0], 0)
        with open(target) as fh:
            self.assertEqual(fh.read(), cli.SHIM)
        self.assertEqual(self.run_cli(None, "shim", "--path", target)[0], 0)  # a shim is replaced freely
        self.assertEqual(os.listdir(os.path.dirname(target)), ["passnote"])  # no temp file left

    def test_install_replaces_a_symlink_not_its_target(self):
        real = os.path.join(self.tmp, "real-passnote")
        with open(real, "w") as fh:
            fh.write("#!/bin/sh\necho real\n")
        target = os.path.join(self.tmp, "bin", "passnote")
        os.makedirs(os.path.dirname(target))
        os.symlink(real, target)
        self.assertEqual(self.run_cli(None, "shim", "--path", target, "--force")[0], 0)
        with open(real) as fh:
            self.assertIn("echo real", fh.read())
        self.assertFalse(os.path.islink(target))

    def test_path_check_compares_real_paths(self):
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        alias = os.path.join(self.tmp, "alias")
        os.symlink(bindir, alias)
        target = os.path.join(bindir, "passnote")
        _, out, _ = self.run_cli(None, "shim", "--path", target, PATH=alias + os.pathsep + "/usr/bin")
        self.assertNotIn("note: add", out)
        _, out, _ = self.run_cli(None, "shim", "--path", target, PATH="/usr/bin")
        self.assertIn("note: add", out)


if __name__ == "__main__":
    unittest.main()
