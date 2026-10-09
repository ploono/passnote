import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from support import BIN, HomeCase, hook_input, join, new_sid, post
from passnote import cursor, hook, paths, render, rooms, sessions, store


class DeliverCase(HomeCase):
    def setUp(self):
        super().setUp()
        paths.ensure_home()
        self.a, self.b, self.c = new_sid(), new_sid(), new_sid()
        join(self.a, "r", "alice")
        join(self.b, "r", "bob")

    def deliver(self, sid, event="PostToolBatch", mode="default", env=None, **extra):
        out = hook.main(event, hook_input(sid, event, mode, **extra), env or self.env(sid))
        return json.loads(out) if out else None

    @staticmethod
    def context(out):
        return (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")

    @staticmethod
    def ids_in(ctx):
        return {line.split(" ", 1)[0] for line in ctx.split("\n")[1:] if line and not line.startswith("…")}

    def drain(self, sid, limit=40):
        seen, contexts = set(), []
        for _ in range(limit):
            out = self.deliver(sid)
            if out is None:
                break
            contexts.append(self.context(out))
            seen |= self.ids_in(contexts[-1])
        return seen, contexts

    def errors_log(self):
        try:
            with open(os.path.join(self.home, "errors.log")) as fh:
                return [json.loads(line) for line in fh]
        except FileNotFoundError:
            return []

    def hold_events(self):
        return [ev for ev in store.read_events("r") if ev.get("type") == "hold"]


class DeliverTest(DeliverCase):
    def test_delivers_new_messages_once(self):
        post(self.a, "r", "hello bob", kind="ask", to=["bob"])
        out = self.deliver(self.b)
        self.assertIn(render.HEADER + "\na1 alice→you ask: hello bob", self.context(out))
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolBatch")
        self.assertEqual(out["systemMessage"], "passnote[r]: 1 from alice (ask a1)")
        self.assertIsNone(self.deliver(self.b))

    def test_filters_own_status_and_other_addressees(self):
        join(self.c, "r", "carol")
        post(self.b, "r", "my own")
        post(self.a, "r", "busy", kind="status")
        post(self.a, "r", "for carol only", to=["carol"])
        self.assertIsNone(self.deliver(self.b))
        self.assertIn("for carol only", self.context(self.deliver(self.c)))

    def test_unjoined_session_and_garbage_input_are_silent(self):
        self.assertEqual(hook.main("PostToolBatch", hook_input(self.c), self.env(self.c)), "")
        self.assertEqual(hook.main("PostToolBatch", "not json", self.env(self.b)), "")

    def test_subagent_fire_does_not_consume_parent_cursor(self):
        post(self.a, "r", "hi")
        before = cursor.load(self.b, "r")
        self.assertIsNone(self.deliver(self.b, agent_id="sub-1", agent_type="general-purpose"))
        self.assertEqual(cursor.load(self.b, "r"), before)
        self.assertIn("hi", self.context(self.deliver(self.b, agent_type="general-purpose")))

    def test_permission_mode_recorded_from_input(self):
        self.deliver(self.b, mode="acceptEdits")
        self.assertEqual(sessions.load_meta(self.b)["permission_mode"], "acceptEdits")

    def test_mode_mismatch_holds_in_both_directions(self):
        post(self.a, "r", "please git push", mode="bypassPermissions")
        out = self.deliver(self.b)
        self.assertNotIn("hookSpecificOutput", out)
        self.assertIn("1 held (permission-mode mismatch)", out["systemMessage"])
        self.assertEqual(store.read_events("r")[-1]["type"], "hold")
        post(self.a, "r", "from a prompting sender", mode="default")
        self.assertNotIn("hookSpecificOutput", self.deliver(self.b, mode="bypassPermissions"))
        post(self.a, "r", "allowed now", mode="bypassPermissions")
        out = self.deliver(self.b, mode="default", env=self.env(self.b, PASSNOTE_ALLOW_BYPASS="1"))
        self.assertIn("allowed now", self.context(out))

    def test_unknown_sender_mode_falls_back_to_recorded_mode(self):
        post(self.a, "r", "posted in the same command as join", mode="unknown")
        self.assertIn("same command as join", self.context(self.deliver(self.b)))
        meta = sessions.load_meta(self.a)
        meta["permission_mode"] = "bypassPermissions"
        sessions.save_meta(self.a, meta)
        post(self.a, "r", "now a bypass sender", mode="unknown")
        self.assertNotIn("hookSpecificOutput", self.deliver(self.b))

    def test_budget_overflow_goes_to_backlog_then_next_fire(self):
        ids = {post(self.a, "r", f"{i}:" + "x" * 400)["id"] for i in range(10)}
        seen, contexts = self.drain(self.b)
        self.assertIn("not shown yet:", contexts[0])
        self.assertTrue(all(len(ctx) <= 2300 for ctx in contexts))
        self.assertEqual(seen, ids)

    def test_addressed_ask_is_prioritized(self):
        for i in range(5):
            post(self.a, "r", f"{i}:" + "x" * 500)
        ask = post(self.a, "r", "quick question", kind="ask", to=["bob"])
        first = self.context(self.deliver(self.b)).split("\n")[1]
        self.assertTrue(first.startswith(ask["id"] + " "))

    def test_unconfirmed_emission_is_redelivered_once(self):
        path = os.path.join(self.tmp, "t.jsonl")
        open(path, "w").close()
        post(self.a, "r", "hello")
        self.assertIn("a1 ", self.context(self.deliver(self.b, transcript_path=path)))
        self.assertIn("a1 ", self.context(self.deliver(self.b, transcript_path=path)))
        self.assertIsNone(self.deliver(self.b, transcript_path=path))

    def test_confirmed_emission_is_not_redelivered(self):
        path = os.path.join(self.tmp, "t.jsonl")
        open(path, "w").close()
        post(self.a, "r", "hello")
        out = self.deliver(self.b, transcript_path=path)
        record = {"type": "attachment", "attachment": {"type": "hook_additional_context", "content": [self.context(out)]}}
        with open(path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
        self.assertIsNone(self.deliver(self.b, transcript_path=path))

    @staticmethod
    def confirm(path, out):
        """Record `out`'s context in the transcript, as Claude Code does when it reaches the model."""
        record = {"type": "attachment", "attachment": {"type": "hook_additional_context",
                                                       "content": [DeliverTest.context(out)]}}
        with open(path, "a") as fh:
            fh.write(json.dumps(record) + "\n")

    def test_a_delivered_id_from_another_room_does_not_confirm_a_dropped_one(self):
        join(self.a, "s", "alice")
        join(self.b, "s", "bob")
        path = os.path.join(self.tmp, "t.jsonl")
        open(path, "w").close()
        post(self.a, "r", "in r")
        self.confirm(path, self.deliver(self.b, transcript_path=path))  # "a1 alice→all say: in r" arrived
        post(self.a, "s", "in s")
        self.assertIn("a1 alice→all say: in s", self.context(self.deliver(self.b, transcript_path=path)))  # dropped
        redelivered = self.context(self.deliver(self.b, transcript_path=path))
        self.assertIn("a1 alice→all say: in s", redelivered)
        self.assertNotIn("in r", redelivered)
        self.assertIsNone(self.deliver(self.b, transcript_path=path))  # once

    def test_a_clipped_and_escaped_line_is_confirmed_as_emitted(self):
        path = os.path.join(self.tmp, "t.jsonl")
        open(path, "w").close()
        post(self.a, "r", "line1\nline2 <tag> \x1b[31mred " + "z" * 900)
        out = self.deliver(self.b, transcript_path=path)
        line = self.context(out).split("\n")[1]
        self.assertIn("line1\\nline2 ‹tag› red z", line)
        self.assertIn("chars: passnote read --id a1)", line)
        self.assertEqual([ref.get("line") for ref in sessions.load_emit(self.b)["emitted"]], [line])
        self.confirm(path, out)
        self.assertIsNone(self.deliver(self.b, transcript_path=path))

    def test_emit_state_from_older_code_is_still_confirmed_by_id(self):
        path = os.path.join(self.tmp, "t.jsonl")
        open(path, "w").close()
        post(self.a, "r", "hello")
        out = self.deliver(self.b, transcript_path=path)
        state = sessions.load_emit(self.b)
        sessions.save_emit(self.b, [{k: v for k, v in ref.items() if k != "line"} for ref in state["emitted"]],
                           state["overflow"], state["ahead"])
        self.confirm(path, out)
        self.assertIsNone(self.deliver(self.b, transcript_path=path))

    def test_deleted_log_is_recreated_and_delivery_resumes(self):
        post(self.a, "r", "before-before-before-before")
        self.deliver(self.b)
        os.unlink(store.log_path("r"))
        self.assertIsNone(self.deliver(self.b))
        post(self.a, "r", "after")
        self.assertIn("after", self.context(self.deliver(self.b)))

    def test_backlog_over_read_cap_is_delivered_across_fires(self):
        with mock.patch.object(store, "MAX_READ", 2048):
            ids = {post(self.a, "r", f"{i}:" + "y" * 250)["id"] for i in range(20)}
            seen, contexts = self.drain(self.b)
        self.assertEqual(seen, ids)
        self.assertGreater(len(contexts), 2)

    def test_concurrent_hooks_never_lose_messages(self):
        env = self.env(self.b)
        payload = hook_input(self.b)
        outputs, codes, samples = [], [], []
        sampling = threading.Event()

        def run_hook():
            res = subprocess.run([sys.executable, "-I", BIN, "hook", "PostToolBatch"], input=payload,
                                 env=env, capture_output=True, text=True, timeout=60)
            codes.append(res.returncode)
            outputs.append(res.stdout)

        def sample_cursor():
            while not sampling.is_set():
                cur = cursor.load(self.b, "r")
                if cur:
                    samples.append((cur["off"], cur["seq"]))
                time.sleep(0.002)

        sampler = threading.Thread(target=sample_cursor)
        sampler.start()
        ids, threads = set(), []
        for i in range(40):
            ids.add(post(self.a, "r", f"m{i}")["id"])
            if i % 4 == 0:
                thread = threading.Thread(target=run_hook)
                thread.start()
                threads.append(thread)
        for thread in threads:
            thread.join()
        seen, _ = self.drain(self.b)
        sampling.set()
        sampler.join()
        for out in outputs:
            if out.strip():
                seen |= self.ids_in(self.context(json.loads(out)))
        self.assertEqual(set(codes), {0})
        self.assertEqual(seen, ids)
        # Every saved cursor moves forward only: its off and its seq never decrease (M15).
        final = cursor.load(self.b, "r")
        samples.append((final["off"], final["seq"]))
        for earlier, later in zip(samples, samples[1:]):
            self.assertLessEqual(earlier[0], later[0])
            self.assertLessEqual(earlier[1], later[1])

    def test_failure_is_logged_and_reported_once(self):
        def boom(*args):
            raise RuntimeError("boom")
        with mock.patch.dict(hook.HANDLERS, {"PostToolBatch": boom}):
            first = hook.main("PostToolBatch", hook_input(self.b), self.env(self.b))
            second = hook.main("PostToolBatch", hook_input(self.b), self.env(self.b))
        self.assertIn("passnote doctor", json.loads(first)["systemMessage"])
        self.assertEqual(second, "")
        self.assertEqual(sessions.load_meta(self.b)["last_error"]["error"], "RuntimeError")
        with open(os.path.join(self.home, "errors.log")) as fh:
            logged = fh.read()
        self.assertIn("RuntimeError", logged)
        self.assertNotIn("boom", logged)  # the exception's type only, never its message (S8)

    def test_entrypoint_hook_mode_always_exits_zero(self):
        res = subprocess.run([sys.executable, "-I", BIN, "hook", "PostToolBatch"], input="garbage",
                             env=self.env(self.b), capture_output=True, text=True, timeout=30)
        self.assertEqual((res.returncode, res.stdout), (0, ""))


class DeliverRulingsTest(DeliverCase):
    """Behaviour the controller's Task 12 rulings add to the brief (numbers in task-12-rulings.md)."""

    def append_raw(self, record):
        with open(store.log_path("r"), "ab") as fh:
            fh.write(json.dumps(record).encode("ascii") + b"\n")

    def forged(self, **fields):
        base = {"v": 1, "seq": 90, "id": "a90", "from": "alice", "sid": self.a, "to": "all",
                "kind": "say", "text": "forged", "mode": "default"}
        base.update(fields)
        return base

    def run_entrypoint(self, sid):
        return subprocess.run([sys.executable, "-I", BIN, "hook", "PostToolBatch"], input=hook_input(sid).encode(),
                              env=self.env(sid), capture_output=True, timeout=30)

    # 18: the whole hook output stays under 8 KB whatever the text.
    def test_emoji_output_stays_under_8kb_and_everything_arrives(self):
        ids = {post(self.a, "r", "\U0001f44b" * 590, to=["bob"])["id"] for _ in range(5)}
        seen = set()
        for _ in range(20):
            out = hook.main("PostToolBatch", hook_input(self.b), self.env(self.b))
            if not out:
                break
            self.assertLess(len(out.encode("utf-8")) + 1, 8192)
            seen |= self.ids_in(self.context(json.loads(out)))
        self.assertEqual(seen, ids)

    def test_emoji_output_with_held_messages_stays_under_8kb(self):
        for _ in range(3):
            post(self.a, "r", "\U0001f44b" * 590, to=["bob"], mode="bypassPermissions")
        ids = {post(self.a, "r", "\U0001f44b" * 590, to=["bob"])["id"] for _ in range(5)}
        res = self.run_entrypoint(self.b)
        self.assertEqual(res.returncode, 0)
        self.assertLess(len(res.stdout), 8192)
        first = json.loads(res.stdout)
        self.assertIn("3 held (permission-mode mismatch)", first["systemMessage"])
        seen = self.ids_in(self.context(first))
        more, _ = self.drain(self.b)
        self.assertEqual(seen | more, ids)

    # 1, 2, 19: malformed lines are skipped, never fatal, and never become a cursor position.
    def test_malformed_lines_are_skipped_and_later_messages_still_arrive(self):
        first = post(self.a, "r", "hello one")
        self.append_raw(self.forged(seq="91"))
        self.append_raw(self.forged(seq=92, sid=[self.a]))
        last = post(self.a, "r", "hello two")
        members = paths.read_json(store.members_path("r"))
        members[self.a] = {"name": {"forged": True}, "alias": "a"}
        store.save_members("r", members)
        ctx = self.context(self.deliver(self.b))
        self.assertEqual(self.ids_in(ctx), {first["id"], last["id"]})
        self.assertNotIn("forged", ctx)
        self.assertIn("alice (unverified)", ctx)
        self.assertIsNone(self.deliver(self.b))

    def test_malformed_final_line_is_not_redelivered(self):
        msg = post(self.a, "r", "hello")
        end = os.path.getsize(store.log_path("r"))
        self.append_raw(self.forged(seq=msg["seq"] + 1, sid=[self.a]))
        self.assertIn("hello", self.context(self.deliver(self.b)))
        for _ in range(2):
            self.assertIsNone(self.deliver(self.b))
        self.assertEqual(cursor.load(self.b, "r")["off"], end)
        self.assertNotIn("cursor-reset", [ev.get("type") for ev in store.read_events("r")])

    # 3: a stalled read (a forged run of oversized lines past the read bound) keeps the cursor.
    def test_read_stall_keeps_the_cursor(self):
        with mock.patch.object(store, "MAX_READ", 1024), mock.patch.object(store, "MAX_SKIP", 4096):
            post(self.a, "r", "before")
            self.assertIn("before", self.context(self.deliver(self.b)))
            with open(store.log_path("r"), "ab") as fh:
                fh.write((b"x" * 2000 + b"\n") * 3)
            post(self.a, "r", "after")
            stalled = cursor.load(self.b, "r")
            for _ in range(2):
                self.assertIsNone(self.deliver(self.b))
            self.assertEqual(cursor.load(self.b, "r"), stalled)
        self.assertEqual([rec["where"] for rec in self.errors_log()].count("read_from"), 1)

    # 11, 20: held and refused messages are recorded once, advance the cursor, and are only shown to the human.
    def test_refused_and_held_messages_are_recorded_once_and_never_injected(self):
        paths.atomic_write_json(os.path.join(paths.room_dir("r"), "config.json"), {"inbound": "refuse"})
        refused = post(self.a, "r", "refused one", to=["bob"])
        out = self.deliver(self.b)
        self.assertNotIn("hookSpecificOutput", out)
        self.assertIn("1 held (refuse)", out["systemMessage"])
        self.assertEqual(cursor.load(self.b, "r")["seq"], refused["seq"])
        self.assertIsNone(self.deliver(self.b))
        os.unlink(os.path.join(paths.room_dir("r"), "config.json"))
        held = post(self.a, "r", "held one", mode="bypassPermissions")
        self.assertIn("1 held (permission-mode mismatch)", self.deliver(self.b)["systemMessage"])
        self.assertIsNone(self.deliver(self.b))
        self.assertEqual([(ev["id"], ev["reason"], ev["to_sid"]) for ev in self.hold_events()],
                         [(refused["id"], "refuse", self.b), (held["id"], "permission-mode mismatch", self.b)])

    # 9, 21: no receiver mode in the payload and none recorded -> held (fail closed).
    def test_unknown_receiver_mode_holds(self):
        def forget_mode(meta):
            meta["permission_mode"] = None
        sessions.update_meta(self.b, forget_mode)
        post(self.a, "r", "hi")
        payload = json.dumps({"session_id": self.b, "hook_event_name": "PostToolBatch"})
        out = json.loads(hook.main("PostToolBatch", payload, self.env(self.b)))
        self.assertNotIn("hookSpecificOutput", out)
        self.assertIn("1 held (receiver mode unknown)", out["systemMessage"])

    def test_overflow_is_checked_again_when_it_renders(self):
        for i in range(10):
            post(self.a, "r", f"{i}:" + "x" * 400)
        self.assertIn("not shown yet:", self.context(self.deliver(self.b)))
        out = self.deliver(self.b, mode="bypassPermissions")
        self.assertNotIn("hookSpecificOutput", out)
        self.assertIn("held (permission-mode mismatch)", out["systemMessage"])
        self.assertIsNone(self.deliver(self.b))

    # 15: ttl_seconds is resolved on UserPromptSubmit, or on any fire while it is unset.
    def test_ttl_is_resolved_on_first_fire_and_refreshed_on_prompts(self):
        self.deliver(self.b, env=self.env(self.b, ANTHROPIC_API_KEY="k"))
        self.assertEqual(sessions.load_meta(self.b)["ttl_seconds"], 300)
        self.deliver(self.b)
        self.assertEqual(sessions.load_meta(self.b)["ttl_seconds"], 300)
        self.deliver(self.b, event="UserPromptSubmit")
        self.assertEqual(sessions.load_meta(self.b)["ttl_seconds"], 3600)

    def test_title_name_is_refreshed_on_prompts_only(self):
        def from_title(meta):
            meta["name_source"] = "title"
        sessions.update_meta(self.b, from_title)
        self.deliver(self.b, session_title="Bob the Builder")
        self.assertEqual(sessions.load_meta(self.b)["name"], "bob")
        self.deliver(self.b, event="UserPromptSubmit", session_title="Bob the Builder")
        self.assertEqual(sessions.load_meta(self.b)["name"], "Bob-the-Builder")
        self.assertEqual(store.load_members("r")[self.b]["name"], "Bob-the-Builder")

    # 16: a busy session lock or meta lock is left to the next fire, silently.
    def test_busy_locks_are_left_to_the_next_fire(self):
        post(self.a, "r", "hi")
        with sessions.session_lock(self.b):
            self.assertEqual(hook.main("PostToolBatch", hook_input(self.b), self.env(self.b)), "")
        with mock.patch.object(sessions, "META_LOCK_TIMEOUT", 0.05), sessions.meta_lock(self.b):
            self.assertEqual(hook.main("PostToolBatch", hook_input(self.b, mode="plan"), self.env(self.b)), "")
        self.assertEqual(self.errors_log(), [])
        self.assertIn("hi", self.context(self.deliver(self.b)))

    # 17a: an unreadable transcript skips the confirmation and is logged once.
    def test_unreadable_transcript_is_logged_once_and_not_redelivered(self):
        missing = os.path.join(self.tmp, "missing.jsonl")
        post(self.a, "r", "one")
        self.assertIn("one", self.context(self.deliver(self.b, transcript_path=missing)))
        post(self.a, "r", "two")
        ctx = self.context(self.deliver(self.b, transcript_path=missing))
        self.assertEqual(self.ids_in(ctx), {"a2"})
        post(self.a, "r", "three")
        self.deliver(self.b, transcript_path=missing)
        self.assertEqual([rec.get("note") for rec in self.errors_log()], [hook.TRANSCRIPT_UNREADABLE])

    # 17b: a transcript_path that isn't a str (an fd, to os.stat) is never used.
    def test_non_string_transcript_path_is_ignored(self):
        post(self.a, "r", "hi")
        self.assertIn("hi", self.context(self.deliver(self.b, transcript_path=1)))
        self.assertIsNone(sessions.load_meta(self.b)["transcript_path"])

        def forge(meta):
            meta["transcript_path"] = 1
        sessions.update_meta(self.b, forge)
        with mock.patch.object(hook.transcript, "unconfirmed", return_value=None) as check:
            self.deliver(self.b, event="UserPromptSubmit")
        check.assert_called_once()
        self.assertIsNone(check.call_args[0][0])

    def test_two_rooms_share_one_fire(self):
        join(self.a, "s", "alice")
        join(self.b, "s", "bob")
        post(self.a, "r", "in r")
        post(self.a, "s", "in s")
        out = self.deliver(self.b)
        ctx = self.context(out)
        self.assertIn("[r] a1 alice→all say: in r", ctx)
        self.assertIn("[s] a1 alice→all say: in s", ctx)
        self.assertIn("passnote[r]: 1 from alice", out["systemMessage"])
        self.assertIn("passnote[s]: 1 from alice", out["systemMessage"])
        self.assertIsNone(self.deliver(self.b))


class DeliverFixRoundTest(DeliverCase):
    """Task 12 review fix round 1."""

    @staticmethod
    def ordered_ids(ctx):
        return [line.split(" ", 1)[0] for line in ctx.split("\n")[1:] if line and not line.startswith("…")]

    def emit_state_size(self, sid):
        path = os.path.join(paths.session_dir(sid), "emit.json")
        return os.path.getsize(path) if os.path.exists(path) else 0

    # Important 1: the carried overflow is capped; the log itself is the backlog.
    def test_large_backlog_keeps_each_fire_bounded_and_loses_nothing(self):
        with mock.patch.object(hook, "OVERFLOW_CAP", 8), mock.patch.object(store, "MAX_READ", 8192):
            posted = [post(self.a, "r", f"{i}:" + "x" * 400)["id"] for i in range(60)]
            delivered = []
            for _ in range(60):
                out = self.deliver(self.b)
                if out is None:
                    break
                delivered += self.ordered_ids(self.context(out))
                self.assertLessEqual(len(sessions.load_emit(self.b)["overflow"]), 8)
                # The emitted refs' lines are this fire's context lines, bounded by the render
                # caps; everything else in the emit state is bounded by the overflow cap.
                lines = sum(len(ref["line"]) for ref in sessions.load_emit(self.b)["emitted"])
                self.assertLessEqual(lines, len(self.context(out)))
                self.assertLess(self.emit_state_size(self.b) - lines, 2000)
        self.assertEqual(delivered, posted)

    def test_rooms_share_the_overflow_cap(self):
        join(self.a, "s", "alice")
        join(self.b, "s", "bob")
        with mock.patch.object(hook, "OVERFLOW_CAP", 8):
            for i in range(40):
                post(self.a, "r", f"{i}:" + "x" * 400)
            self.deliver(self.b)
            post(self.a, "s", "quiet room")
            ctx = "".join(self.context(self.deliver(self.b)) for _ in range(2))
        self.assertIn("quiet room", ctx)

    # Important 2: a hook whose stdout reader is gone still exits 0, silently.
    def test_entrypoint_exits_zero_when_stdout_is_closed(self):
        for i in range(5):
            post(self.a, "r", "hello " + "z" * 500)
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            proc = subprocess.Popen([sys.executable, "-I", BIN, "hook", "PostToolBatch"], stdin=subprocess.PIPE,
                                    stdout=write_end, stderr=subprocess.PIPE, env=self.env(self.b))
        finally:
            os.close(write_end)
        _, err = proc.communicate(hook_input(self.b).encode(), timeout=30)
        self.assertEqual((proc.returncode, err), (0, b""))

    # (a) The name senders address is the member name in that room.
    def test_receiver_name_is_taken_per_room(self):
        members = store.load_members("r")
        members[self.b]["name"] = "robert"  # renamed in the room; the meta update was missed
        store.save_members("r", members)
        post(self.a, "r", "for robert", to=["robert"])
        self.assertIn("a1 alice→you say: for robert", self.context(self.deliver(self.b)))

    # (b) An unconfirmed emission that would now be held is held, not dropped.
    def test_unconfirmed_emission_now_held_is_held(self):
        path = os.path.join(self.tmp, "t.jsonl")
        open(path, "w").close()
        post(self.a, "r", "hello")
        self.assertIn("a1 ", self.context(self.deliver(self.b, transcript_path=path)))
        out = self.deliver(self.b, mode="bypassPermissions", transcript_path=path)
        self.assertNotIn("hookSpecificOutput", out)
        self.assertIn("1 held (permission-mode mismatch)", out["systemMessage"])
        self.assertEqual([ev["id"] for ev in self.hold_events()], ["a1"])

    # (c) A full read window that moves no cursor is logged once.
    def test_read_window_without_progress_is_logged_once(self):
        with mock.patch.object(store, "MAX_READ", 2048):
            post(self.a, "r", "first")
            self.deliver(self.b)
            with open(store.log_path("r"), "ab") as fh:
                fh.write(b"junk\n" * 1000)
            post(self.a, "r", "after junk")
            for _ in range(3):
                self.assertIsNone(self.deliver(self.b))
        notes = [rec.get("note") for rec in self.errors_log()]
        self.assertEqual(notes.count(hook.READ_NO_PROGRESS), 1)

    # (d) A joiner after an unverifiable tail still gets what is posted next.
    def test_joiner_after_an_unverifiable_tail_receives_new_messages(self):
        with mock.patch.object(store, "MAX_READ", 2048):
            for i in range(30):
                post(self.a, "r", f"old{i} " + "y" * 100)
            with open(store.log_path("r"), "ab") as fh:
                fh.write(b"x" * 3000 + b"\n")
            join(self.c, "r", "carol")
            post(self.a, "r", "new for carol", to=["carol"])
            self.assertIn("new for carol", self.context(self.deliver(self.c)))


class RoomIdsCase(DeliverCase):
    """Helpers that keep each delivered id with its room and its fire."""

    @staticmethod
    def room_ids(ctx, default_room="r"):
        """[(room, id)] in render order; lines without a [room] prefix belong to default_room."""
        out = []
        for line in ctx.split("\n")[1:]:
            if not line or line.startswith("…"):
                continue
            room = default_room
            if line.startswith("["):
                room, line = line[1:].split("] ", 1)
            out.append((room, line.split(" ", 1)[0]))
        return out

    def drain_ids(self, sid, default_room="r", limit=200):
        fires = []
        for _ in range(limit):
            out = self.deliver(sid)
            if out is None:
                break
            fires.append(self.room_ids(self.context(out), default_room))
        return fires


class AddressedClipTest(DeliverCase):
    def test_an_addressed_report_of_1500_chars_arrives_whole(self):
        post(self.a, "r", "r" * 1500, kind="done", to=["bob"])
        ctx = self.context(self.deliver(self.b))
        self.assertIn("r" * 1500, ctx)
        self.assertNotIn("passnote read --id", ctx)

    def test_a_broadcast_keeps_the_600_char_clip(self):
        post(self.a, "r", "b" * 1500)
        self.assertIn("b" * 600 + "… (+900 chars: passnote read --id a1)", self.context(self.deliver(self.b)))

    def test_the_addressed_clip_is_configurable(self):
        os.environ["PASSNOTE_CLIP_ADDRESSED_CHARS"] = "800"
        post(self.a, "r", "r" * 1500, to=["bob"])
        self.assertIn("(+700 chars: passnote read --id a1)", self.context(self.deliver(self.b)))

    def test_a_larger_clip_chars_wins_for_addressed_messages(self):
        os.environ["PASSNOTE_CLIP_CHARS"] = "1800"
        os.environ["PASSNOTE_CLIP_ADDRESSED_CHARS"] = "1500"
        post(self.a, "r", "r" * 1700, to=["bob"])
        self.assertIn("r" * 1700, self.context(self.deliver(self.b)))


class FullTextDeliverTest(DeliverCase):
    def test_a_long_post_is_delivered_with_its_path(self):
        me = store.load_members("r")[self.a]
        store.append_message("r", {"from": "alice", "sid": self.a, "to": ["bob"], "kind": "done", "text": "q" * 4000,
                                   "mode": "default"}, me["alias"], full_text="q" * 6000)
        ctx = self.context(self.deliver(self.b))
        self.assertIn("q" * 1500 + f"… (+4500 chars: full text in {store.full_text_path('r', 'a1')})", ctx)

    def test_every_forged_full_chars_shape_is_still_delivered(self):
        forged = ("5000", True, -1, 1.5, [5000], {"n": 5000}, 0)
        for value in forged:
            post(self.a, "r", "f" * 700, full_chars=value)
        seen, contexts = self.drain(self.b)
        ids = [f"a{i}" for i in range(1, len(forged) + 1)]
        self.assertEqual(seen, set(ids))
        text = "\n".join(contexts)
        for msg_id in ids:
            self.assertIn("f" * 600 + f"… (+100 chars: passnote read --id {msg_id})", text)
        self.assertNotIn("full text in", text)

    def test_forged_full_chars_keep_the_output_under_8kb(self):
        for i in range(60):
            post(self.a, "r", "漢" * 4000, full_chars=10 ** 12, to=["bob"], kind="ask")
        out = hook.main("PostToolBatch", hook_input(self.b), self.env(self.b))
        self.assertLess(len(out.encode()), 8192)


class DeliverFixRound2Test(RoomIdsCase):
    """Task 12 review fix round 2."""

    # 1. A reset onto a replaced copy of the log resumes past the old lines.
    def test_reset_onto_a_replaced_log_delivers_new_lines_once(self):
        with mock.patch.object(store, "MAX_READ", 2048):
            for i in range(40):
                post(self.a, "r", f"old{i} " + "y" * 100)
            self.drain(self.b)
            log = store.log_path("r")
            with open(log, "rb") as src, open(log + ".tmp", "wb") as dst:
                dst.write(src.read())
            os.replace(log + ".tmp", log)  # same content, new inode
            self.assertIsNone(self.deliver(self.b))  # the reset alone redelivers nothing
            new = post(self.a, "r", "new after replace")
            fires = self.drain_ids(self.b)
        self.assertEqual(fires, [[("r", new["id"])]])
        self.assertEqual(cursor.load(self.b, "r")["seq"], new["seq"])

    # 2. An addressed ask deep in a backlog is taken ahead of a full room share, once.
    def test_deep_addressed_ask_is_taken_ahead_once(self):
        with mock.patch.object(hook, "OVERFLOW_CAP", 8):
            posted = [post(self.a, "r", f"{i}:" + "x" * 300)["id"] for i in range(40)]
            ask = post(self.a, "r", "deep question", kind="ask", to=["bob"])["id"]
            posted += [post(self.a, "r", f"tail{i}")["id"] for i in range(3)]
            fires = self.drain_ids(self.b)
        self.assertIn(("r", ask), fires[0])
        delivered = [mid for fire in fires for _, mid in fire]
        self.assertEqual(sorted(delivered), sorted(posted + [ask]))  # each exactly once
        self.assertEqual([mid for mid in delivered if mid != ask], posted)  # the rest in order
        self.assertEqual(sessions.load_emit(self.b)["ahead"], [])

    # Minor: carried refs left unread still count toward their room's share, so newer lines in
    # that room can't overtake them.
    def test_unread_carried_refs_keep_their_place_in_the_room(self):
        join(self.a, "s", "alice")
        join(self.b, "s", "bob")
        for room, count in (("s", 3), ("r", 2)):
            for i in range(count):
                post(self.a, room, f"{room}{i}")
        refs, cursors = [], {}
        for room in ("s", "r"):
            pos = 0
            with open(store.log_path(room), "rb") as fh:
                for raw in fh:
                    msg = json.loads(raw)
                    refs.append({"room": room, "off": pos, "len": len(raw) - 1, "id": msg["id"]})
                    pos += len(raw)
                    cursors[room] = {"ino": os.stat(store.log_path(room)).st_ino, "off": pos, "seq": msg["seq"]}
        for room, cur in cursors.items():
            cursor.save(self.b, room, cur)
        sessions.save_emit(self.b, [], refs)
        newer = post(self.a, "r", "r-newer")["id"]
        with mock.patch.object(hook, "OVERFLOW_CAP", 4):
            fires = self.drain_ids(self.b)
        in_r = [mid for fire in fires for room, mid in fire if room == "r"]
        self.assertEqual(in_r, ["a1", "a2", newer])


class DeliverFixRound3Test(RoomIdsCase):
    """Task 12 review fix round 3."""

    def test_forged_low_seq_tail_is_never_the_resume_point(self):
        for i in range(40):
            post(self.a, "r", f"old{i}")
        self.drain(self.b)
        log = store.log_path("r")
        with open(log, "rb") as fh:
            data = fh.read()
        os.unlink(log)
        with open(log, "wb") as fh:  # replaced without its first line: not a copy
            fh.write(data.split(b"\n", 1)[1])
        new = [post(self.a, "r", f"new{i}")["id"] for i in range(10)]
        forged = {"v": 1, "seq": 5, "id": "z5", "from": "alice", "sid": self.a, "to": "all",
                  "kind": "say", "text": "forged", "mode": "default"}
        with open(log, "ab") as fh:
            fh.write(json.dumps(forged).encode("ascii") + b"\n")
        fires = self.drain_ids(self.b)
        self.assertEqual([mid for fire in fires for _, mid in fire], new)
        self.assertEqual(cursor.load(self.b, "r")["seq"], 50)

    def test_ahead_refs_do_not_outlive_a_restarted_log(self):
        with mock.patch.object(hook, "OVERFLOW_CAP", 8):
            for i in range(40):
                post(self.a, "r", f"{i}:" + "x" * 50)
            ask = post(self.a, "r", "deep q", kind="ask", to=["bob"])["id"]
            self.assertIn(("r", ask), self.room_ids(self.context(self.deliver(self.b))))
            os.unlink(store.log_path("r"))  # restart: ids are reused
            new = [post(self.a, "r", f"N{i}")["id"] for i in range(45)]
            delivered = [mid for fire in self.drain_ids(self.b) for _, mid in fire]
        self.assertIn(ask, new)
        self.assertIn(ask, delivered)  # the new message with the reused id is not skipped



class DeliverFixRound4Test(RoomIdsCase):
    """Task 12 review fix round 4: a resume point is a position the hook itself could have saved."""

    def replace_log_and_post(self, room):
        """40 messages delivered to bob, the log replaced without its first line (not a copy),
        then a41..a50 posted: returns their ids."""
        join(self.a, room, "alice")
        join(self.b, room, "bob")
        for i in range(40):
            post(self.a, room, f"old{i}")
        self.drain(self.b)
        self.assertEqual(cursor.load(self.b, room)["seq"], 40)
        log = store.log_path(room)
        with open(log, "rb") as fh:
            data = fh.read()
        os.unlink(log)
        with open(log, "wb") as fh:
            fh.write(data.split(b"\n", 1)[1])
        return [post(self.a, room, f"new{i}")["id"] for i in range(10)]

    def forged(self, seq):
        return json.dumps({"v": 1, "seq": seq, "id": f"z{seq}", "from": "alice", "sid": self.a, "to": "all",
                           "kind": "say", "text": "forged", "mode": "default"}).encode("ascii")

    def drain_watching_the_cursor(self, room):
        """Delivered ids, and bob's cursor seq after each fire."""
        delivered, seqs = [], []
        for _ in range(50):
            out = self.deliver(self.b)
            seqs.append(cursor.load(self.b, room)["seq"])
            if out is None:
                break
            delivered += [mid for _, mid in self.room_ids(self.context(out), room)]
        return delivered, seqs

    def test_forged_descending_then_ascending_tail_is_never_the_resume_point(self):
        tails = [
            ("messages-5-6", lambda: [self.forged(5), self.forged(6)]),
            ("bare-5-6", lambda: [b'{"seq": 5}', b'{"seq": 6}']),
            ("messages-7-3-4-5", lambda: [self.forged(7), self.forged(3), self.forged(4), self.forged(5)]),
            ("bare-and-messages", lambda: [b'{"seq": 3}', self.forged(4), b'{"seq": 5}', self.forged(6)]),
        ]
        for room, tail in tails:
            with self.subTest(tail=room):
                new = self.replace_log_and_post(room)
                with open(store.log_path(room), "ab") as fh:
                    fh.write(b"".join(line + b"\n" for line in tail()))
                delivered, seqs = self.drain_watching_the_cursor(room)
                self.assertEqual(delivered, new)
                self.assertEqual(seqs[-1], 50)
                self.assertEqual(seqs, sorted(seqs))  # never moves back
                self.assertGreaterEqual(seqs[0], 40)

    def test_a_reset_never_moves_the_cursor_seq_back(self):
        # The copy lost bob's last line (seq 40) and gained an invalid line with a higher seq (not
        # a restart): the resume point after seq 39 is below bob's cursor, and nothing valid
        # follows it, so the cursor is kept (seq 40) rather than moved back to 39.
        for i in range(40):
            post(self.a, "r", f"old{i}")
        self.drain(self.b)
        log = store.log_path("r")
        with open(log, "rb") as fh:
            lines = fh.read().split(b"\n")[:-1]
        os.unlink(log)
        with open(log, "wb") as fh:
            fh.write(b"".join(line + b"\n" for line in lines[:39]) + b'{"seq": 99}\n')
        for _ in range(2):
            self.assertIsNone(self.deliver(self.b))
            self.assertEqual(cursor.load(self.b, "r")["seq"], 40)
        nxt = post(self.a, "r", "next")["id"]
        self.assertEqual([mid for fire in self.drain_ids(self.b) for _, mid in fire], [nxt])
        self.assertEqual(cursor.load(self.b, "r")["seq"], 100)



class ReceiptTest(DeliverCase):
    def seen_line(self, sid):
        lines = [line for line in self.context(self.deliver(sid)).split("\n") if line.startswith("passnote: seen by")]
        return lines[0] if lines else None

    def test_the_sender_sees_one_line_after_the_addressee_gets_the_ask(self):
        post(self.a, "r", "review?", kind="ask", to=["bob"])
        self.assertIsNone(self.seen_line(self.a))   # tracked; bob hasn't had a turn
        self.deliver(self.b)
        self.assertEqual(self.seen_line(self.a), "passnote: seen by bob: a1")
        self.assertIsNone(self.seen_line(self.a))   # at most once per message

    def test_a_receipt_alone_is_the_whole_context(self):
        post(self.a, "r", "review?", kind="ask", to=["bob"])
        self.deliver(self.a)
        self.deliver(self.b)
        out = self.deliver(self.a, event="UserPromptSubmit")
        self.assertEqual(out["hookSpecificOutput"], {"hookEventName": "UserPromptSubmit",
                                                     "additionalContext": "passnote: seen by bob: a1"})
        self.assertNotIn("systemMessage", out)
        self.assertEqual(sessions.load_emit(self.a)["emitted"], [])  # never confirmed or redelivered

    def test_props_get_receipts_and_says_and_broadcasts_do_not(self):
        post(self.a, "r", "ship at 3", kind="prop", to=["bob"])
        post(self.a, "r", "fyi", kind="say", to=["bob"])
        post(self.a, "r", "anyone?", kind="ask")
        self.deliver(self.a)
        self.deliver(self.b)
        self.assertEqual(self.seen_line(self.a), "passnote: seen by bob: a1")

    def test_a_held_ask_is_never_reported_seen(self):
        sessions.update_meta(self.b, lambda meta: meta.update(permission_mode="bypassPermissions"))
        post(self.a, "r", "secret?", kind="ask", to=["bob"])
        self.deliver(self.a)
        self.deliver(self.b, mode="bypassPermissions")
        self.assertIsNone(self.seen_line(self.a))
        self.assertEqual(sessions.load_emit(self.a)["receipts"], [])

    def test_a_hold_from_before_the_addressee_cleared_still_counts(self):
        sessions.update_meta(self.b, lambda meta: meta.update(permission_mode="bypassPermissions"))
        post(self.a, "r", "secret?", kind="ask", to=["bob"])
        self.deliver(self.a)
        self.deliver(self.b, mode="bypassPermissions")   # held: the hold event names bob's old sid
        new = new_sid()
        rooms.carry_over(self.b, new)
        sessions.update_meta(new, lambda meta: meta.update(permission_mode="default"))
        self.assertIsNone(self.seen_line(self.a))
        self.assertEqual(sessions.load_emit(self.a)["receipts"], [])

    def test_an_overflowed_ask_is_not_seen_until_it_is_delivered(self):
        os.environ["PASSNOTE_RENDER_BUDGET_CHARS"] = "300"
        join(self.c, "r", "carol")
        for i in range(3):
            post(self.c, "r", "c" * 150, kind="ask", to=["bob"])
        post(self.a, "r", "mine?", kind="ask", to=["bob"])
        self.deliver(self.a)
        self.deliver(self.b)                       # the cursor passes a4; a4 overflows
        self.assertIsNone(self.seen_line(self.a))
        self.drain(self.b)
        self.assertEqual(self.seen_line(self.a), "passnote: seen by bob: a4")

    def test_an_ask_taken_ahead_of_the_cursor_counts_as_seen(self):
        join(self.c, "r", "carol")
        for i in range(hook.OVERFLOW_CAP + 2):
            post(self.c, "r", f"n{i}")
        ask = post(self.a, "r", "deep?", kind="ask", to=["bob"])
        self.drain(self.a)
        self.deliver(self.b)                       # a full share, then the ask taken ahead
        self.assertLess(cursor.load(self.b, "r")["seq"], ask["seq"])
        self.assertEqual(self.seen_line(self.a), f"passnote: seen by bob: {ask['id']}")

    def test_receipts_are_bounded_per_fire(self):
        for i in range(10):
            post(self.a, "r", f"q{i}", kind="ask", to=["bob"])
        self.deliver(self.a)
        self.drain(self.b)
        first, second = self.seen_line(self.a), self.seen_line(self.a)
        self.assertEqual(first.count(", ") + 1, 6)
        self.assertEqual(second.count(", ") + 1, 4)
        self.assertIsNone(self.seen_line(self.a))

    def test_receipts_cut_for_length_come_next_fire(self):
        alias = "a" * 72  # a long id shape (a raw log line can carry one): only three fit a line
        ids = [store.append_message("r", {"from": "alice", "sid": self.a, "to": ["bob"], "kind": "ask",
                                          "text": "q", "mode": "default"}, alias)["id"] for _ in range(6)]
        self.deliver(self.a)
        self.drain(self.b)
        first, second = self.seen_line(self.a), self.seen_line(self.a)
        self.assertLessEqual(len(first), render.RECEIPT_MAX_CHARS)
        self.assertEqual(first, "passnote: seen by bob: " + ", ".join(ids[:3]))
        self.assertEqual(second, "passnote: seen by bob: " + ", ".join(ids[3:]))
        self.assertIsNone(self.seen_line(self.a))

    def test_pending_receipts_are_capped_and_expire(self):
        for i in range(20):
            post(self.a, "r", f"q{i}", kind="ask", to=["bob"])
        self.deliver(self.a)
        pending = sessions.load_emit(self.a)["receipts"]
        self.assertEqual(len(pending), 16)
        self.assertEqual(pending[0]["id"], "a5")    # the oldest four were dropped
        state = sessions.load_emit(self.a)
        aged = [dict(entry, ts=entry["ts"] - 86401) for entry in state["receipts"]]
        sessions.save_emit(self.a, state["emitted"], state["overflow"], state["ahead"], aged)
        self.drain(self.b)
        self.assertIsNone(self.seen_line(self.a))

    def test_an_addressee_who_left_is_dropped(self):
        post(self.a, "r", "q", kind="ask", to=["bob"])
        self.deliver(self.a)
        rooms.leave(self.b, "r")
        self.assertIsNone(self.seen_line(self.a))
        self.assertEqual(sessions.load_emit(self.a)["receipts"], [])

    def test_a_forged_member_key_drops_the_receipt_and_delivery_continues(self):
        post(self.a, "r", "q", kind="ask", to=["bob"])
        self.deliver(self.a)
        join(self.c, "r", "carol")
        with store.room_lock("r"):
            members = store.load_members("r")
            members["not-a-sid"] = dict(members.pop(self.b))  # bob's name, under a forged key
            store.save_members("r", members)
        post(self.c, "r", "still here")
        ctx = self.context(self.deliver(self.a))
        self.assertIn("still here", ctx)
        self.assertNotIn("seen by", ctx)
        self.assertEqual(sessions.load_emit(self.a)["receipts"], [])

    def test_forged_receipt_state_is_dropped_without_an_error(self):
        post(self.b, "r", "hi")
        self.deliver(self.b)                       # bob's cursor passes seq 1
        state, now = sessions.load_emit(self.a), time.time()
        forged = [None, "x", {"room": "r", "id": "a1", "seq": True, "ts": now, "to": ["bob"]},
                  {"room": "elsewhere", "id": "a1", "seq": 1, "ts": now, "to": ["bob"]},
                  {"room": "r", "id": "a1", "seq": 1, "ts": "now", "to": ["bob"]},
                  {"room": "r", "id": "a1", "seq": 1, "ts": now, "to": "bob"},
                  {"room": "r", "id": "a1", "seq": 1, "ts": now, "to": ["böb", 7]},
                  {"room": "r", "id": "../x", "seq": 1, "ts": now, "to": ["bob", "böb"]}]
        sessions.save_emit(self.a, state["emitted"], state["overflow"], state["ahead"], forged)
        ctx = self.context(self.deliver(self.a))
        self.assertIn("hi", ctx)
        self.assertNotIn("seen by", ctx)
        self.assertEqual(sessions.load_emit(self.a)["receipts"], [])
        self.assertEqual(self.errors_log(), [])

    def test_a_forged_emit_state_of_the_addressee_does_not_stop_delivery(self):
        post(self.a, "r", "q", kind="ask", to=["bob"])
        self.deliver(self.a)
        self.deliver(self.b)
        paths.atomic_write_json(os.path.join(paths.session_dir(self.b), "emit.json"),
                                {"emitted": 5, "overflow": 7, "ahead": True, "receipts": "x"})
        post(self.b, "r", "still here")
        ctx = self.context(self.deliver(self.a))
        self.assertIn("still here", ctx)
        self.assertIn("passnote: seen by bob: a1", ctx)
        self.assertEqual(self.errors_log(), [])

    def test_no_receipt_work_when_nothing_is_pending(self):
        post(self.b, "r", "hi")
        with mock.patch.object(cursor, "load", wraps=cursor.load) as spy:
            self.deliver(self.a)
        self.assertEqual({call.args[0] for call in spy.call_args_list}, {self.a})

    def test_receipt_and_heavy_traffic_stay_under_8kb(self):
        for i in range(6):
            post(self.a, "r", "q", kind="ask", to=["bob"])
        self.deliver(self.a)
        self.drain(self.b)
        for i in range(60):
            post(self.b, "r", "漢" * 1500, kind="ask", to=["alice"])
        out = hook.main("PostToolBatch", hook_input(self.a), self.env(self.a))
        self.assertLess(len(out.encode()), 8192)
        self.assertIn("passnote: seen by bob:", json.loads(out)["hookSpecificOutput"]["additionalContext"])

if __name__ == "__main__":
    unittest.main()
