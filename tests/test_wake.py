import os
import time
import unittest

from support import ROOT, HomeCase, new_sid
from passnote import paths, sessions, store, wake

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "transcript_delivered.jsonl")


class TtlTest(HomeCase):
    def test_parse_ttl(self):
        self.assertEqual(wake.parse_ttl("5m"), 300)
        self.assertEqual(wake.parse_ttl("1h"), 3600)
        self.assertEqual(wake.parse_ttl("900"), 900)
        self.assertIsNone(wake.parse_ttl("soon"))
        self.assertIsNone(wake.parse_ttl(None))
        self.assertIsNone(wake.parse_ttl("\u00b2"))
        self.assertIsNone(wake.parse_ttl("1" * 5000))
        self.assertIsNone(wake.parse_ttl("0"))

    def test_transcript_bucket_uses_main_thread_only(self):
        self.assertEqual(wake.transcript_bucket(FIXTURE), "ephemeral_1h")
        self.assertIsNone(wake.transcript_bucket(os.path.join(self.tmp, "missing.jsonl")))
        self.assertIsNone(wake.transcript_bucket(None))

    def test_resolution_order(self):
        self.assertEqual(wake.resolve_ttl({"FORCE_PROMPT_CACHING_5M": "1", "CLAUDE_CODE_PROMPT_CACHE_TTL": "1h"}), 300)
        self.assertEqual(wake.resolve_ttl({"CLAUDE_CODE_PROMPT_CACHE_TTL": "1h"}, settings_ttl="5m"), 3600)
        self.assertEqual(wake.resolve_ttl({}, settings_ttl="5m", bucket="ephemeral_1h"), 300)
        self.assertEqual(wake.resolve_ttl({"ENABLE_PROMPT_CACHING_1H": "true"}, bucket="ephemeral_5m"), 3600)
        self.assertEqual(wake.resolve_ttl({}, bucket="ephemeral_5m"), 300)
        self.assertEqual(wake.resolve_ttl({}), 3600)
        self.assertEqual(wake.resolve_ttl({"ANTHROPIC_API_KEY": "x"}), 300)
        self.assertEqual(wake.resolve_ttl({"FORCE_PROMPT_CACHING_5M": "1"}, override=42), 42)


class DecideTest(HomeCase):
    def setUp(self):
        super().setUp()
        paths.ensure_home()
        self.a, self.b = new_sid(), new_sid()
        paths.makedirs(paths.session_dir(self.b))  # running: no pid recorded counts as running

    def test_eligibility(self):
        ask = {"id": "b1", "sid": self.b, "from": "bob", "to": ["alice"], "kind": "ask"}
        by_id = {"b1": ask}
        self.assertTrue(wake.is_eligible(ask, "alice", self.a, by_id))
        self.assertFalse(wake.is_eligible(dict(ask, to="all"), "alice", self.a, by_id))
        self.assertFalse(wake.is_eligible(dict(ask, kind="say"), "alice", self.a, by_id))
        self.assertTrue(wake.is_eligible(dict(ask, kind="say", wake=True), "alice", self.a, by_id))
        reply = {"id": "a2", "sid": self.a, "from": "alice", "to": ["bob"], "kind": "ans", "re": "b1"}
        self.assertTrue(wake.is_eligible(reply, "bob", self.b, by_id))
        self.assertFalse(wake.is_eligible(dict(reply, re="zz"), "bob", self.b, by_id))
        self.assertFalse(wake.is_eligible(reply, "bob", self.a, by_id))

    def test_a_reply_to_an_ask_from_before_clear_is_eligible(self):
        new_b = new_sid()  # bob after /clear
        ask = {"id": "b1", "sid": self.b, "from": "bob", "to": ["alice"], "kind": "ask"}
        reply = {"id": "a2", "sid": self.a, "from": "alice", "to": ["bob"], "kind": "ans", "re": "b1"}
        members = {self.a: {"name": "alice"}, new_b: {"name": "bob", "prev_sids": [self.b]}}
        self.assertTrue(wake.is_eligible(reply, "bob", new_b, {"b1": ask}, members))
        self.assertFalse(wake.is_eligible(reply, "bob", new_b, {"b1": ask}))

    def test_warm_cold_never_urgent(self):
        breaker = {"max": 3, "minutes": 10}
        self.assertEqual(wake.decide("r", self.b, self.a, False, breaker)[1], "cold (never active; --urgent to force)")
        sessions.touch_active(self.b)
        self.assertEqual(wake.decide("r", self.b, self.a, False, breaker), ("WAKE", "warm"))
        sessions.touch_active(self.b, time.time() - 7200)
        decision, reason = wake.decide("r", self.b, self.a, False, breaker)
        self.assertEqual(decision, "WAIT")
        self.assertEqual(reason, "cold (last active 120m ago; --urgent to force)")
        self.assertEqual(wake.decide("r", self.b, self.a, True, breaker), ("WAKE", "urgent"))

    def test_a_gone_member_is_never_woken_even_warm_or_urgent(self):
        breaker = {"max": 3, "minutes": 10}
        sessions.update_meta(self.b, lambda meta: meta.update(pid=2 ** 31 - 2))  # no such process
        sessions.touch_active(self.b)  # warm
        gone = ("WAIT", "gone (session not running; it will see this when resumed)")
        self.assertEqual(wake.decide("r", self.b, self.a, False, breaker), gone)
        self.assertEqual(wake.decide("r", self.b, self.a, True, breaker), gone)

    def test_breaker_caps_even_urgent(self):
        now = time.time()
        for _ in range(3):
            store.append_event("r", {"type": "wake", "decision": "WAKE", "from_sid": self.a, "to_sid": self.b, "ts": now - 60})
        store.append_event("r", {"type": "wake", "decision": "WAKE", "from_sid": self.a, "to_sid": self.b, "ts": now - 3600})
        breaker = {"max": 3, "minutes": 10}
        self.assertEqual(wake.recent_wakes("r", self.a, self.b, now, 10), 3)
        self.assertEqual(wake.decide("r", self.b, self.a, True, breaker, now)[1], "breaker (3 wakes in 10m)")

    def test_recent_wakes_skips_events_without_a_numeric_ts(self):
        # Any member can forge a raw events line; a bad ts must not crash the next post.
        for ts in ("soon", None, True, [1], {"t": 1}):
            store.append_event("r", {"type": "wake", "decision": "WAKE", "from_sid": self.a, "to_sid": self.b, "ts": ts})
        self.assertEqual(wake.recent_wakes("r", self.a, self.b, time.time(), 10), 0)

    def test_doorbell_quotes_a_forged_id_safely(self):
        line = wake.doorbell_line("bob", {"id": 'a7") rm -rf ~ ("\\', "from": "alice", "text": "hi"})
        self.assertEqual(line.count('"'), 4)  # to="bob" and message="..." only
        self.assertEqual(len(line.splitlines()), 1)

    def test_doorbell_line_carries_no_message_text(self):
        m = {"id": "a7", "from": "alice", "text": 'the codeword is MARIGOLD "now"\nthen more'}
        line = wake.doorbell_line("bob", m)
        self.assertEqual(line, 'WAKE bob: SendMessage(to="bob", message="a7 from alice: passnote note waiting")')
        self.assertNotIn("MARIGOLD", line)

    def test_doorbell_quotes_a_forged_sender_safely(self):
        for sender in ('al"ice', "alice\\", "al\nice", "x" * 500):
            with self.subTest(sender=sender[:8]):
                line = wake.doorbell_line("bob", {"id": "a7", "from": sender, "text": "hi"})
                self.assertEqual(line.count('"'), 4)  # to="bob" and message="..." only
                self.assertEqual(len(line.splitlines()), 1)
                self.assertTrue(line.endswith(': passnote note waiting")'), line)
                self.assertLessEqual(len(line), 200)  # the sender is clipped to render.FIELD_CLIP


if __name__ == "__main__":
    unittest.main()
