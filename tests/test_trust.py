import unittest

from support import HomeCase, new_sid
from passnote import paths, sessions, trust


class TrustTest(unittest.TestCase):
    def test_mode_classes(self):
        for mode in ("default", "acceptEdits", "plan"):
            with self.subTest(mode=mode):
                self.assertEqual(trust.mode_class(mode), "prompting")
        for mode in ("bypassPermissions", "auto", "dontAsk", None, "unknown"):
            with self.subTest(mode=mode):
                self.assertEqual(trust.mode_class(mode), "nonprompting")

    def test_mode_class_handles_unhashable_types(self):
        # Unhashable types should return nonprompting
        self.assertEqual(trust.mode_class([]), "nonprompting")
        self.assertEqual(trust.mode_class({}), "nonprompting")
        self.assertEqual(trust.mode_class(["default"]), "nonprompting")

    def test_hold_matrix_is_symmetric(self):
        self.assertIsNone(trust.hold_reason("default", "acceptEdits", {}))
        self.assertIsNone(trust.hold_reason("bypassPermissions", "auto", {}))
        self.assertEqual(trust.hold_reason("bypassPermissions", "default", {}), "permission-mode mismatch")
        self.assertEqual(trust.hold_reason("default", "bypassPermissions", {}), "permission-mode mismatch")
        self.assertEqual(trust.hold_reason("unknown", "default", {}), "permission-mode mismatch")

    def test_only_env_lifts_a_mismatch_and_config_only_tightens(self):
        allow = {"PASSNOTE_ALLOW_BYPASS": "1"}
        self.assertIsNone(trust.hold_reason("bypassPermissions", "default", allow))
        self.assertEqual(trust.hold_reason("default", "default", allow, "hold"), "inbound=hold")
        self.assertEqual(trust.hold_reason("default", "default", {}, "refuse"), "refuse")
        self.assertEqual(trust.effective_inbound("auto", "hold", None), "hold")
        self.assertEqual(trust.effective_inbound("accept", None), "auto")

    def test_allow_bypass_only_on_exact_value(self):
        # Only "1" lifts the hold; other values don't
        self.assertEqual(trust.hold_reason("bypassPermissions", "default", {"PASSNOTE_ALLOW_BYPASS": "0"}), "permission-mode mismatch")
        self.assertEqual(trust.hold_reason("bypassPermissions", "default", {"PASSNOTE_ALLOW_BYPASS": "true"}), "permission-mode mismatch")
        self.assertEqual(trust.hold_reason("bypassPermissions", "default", {"PASSNOTE_ALLOW_BYPASS": ""}), "permission-mode mismatch")

    def test_refuse_wins_over_env_var(self):
        # Refuse should take precedence even with PASSNOTE_ALLOW_BYPASS
        allow = {"PASSNOTE_ALLOW_BYPASS": "1"}
        self.assertEqual(trust.hold_reason("bypassPermissions", "default", allow, "refuse"), "refuse")

    def test_secret_guard(self):
        # Token shapes are built at runtime: the repo is public, and GitHub push protection scans
        # every pushed file (test_packaging pins that no tracked file holds a whole one).
        positives = [
            # Private keys
            "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
            "-----BEGIN " + "PRIVATE KEY BLOCK-----",
            # AWS keys (both AKIA and ASIA)
            "key " + "AKIA" + "ABCDEFGHIJKLMNOP here",
            "key " + "ASIA" + "ABCDEFGHIJKLMNOP here",
            # API keys in the sk-, sk_live_ and sk_test_ forms (Anthropic, OpenAI, Stripe)
            "sk-" + "ant-api03-abcdefghijklmnopqrstuvwxyz",
            "sk_" + "live_1234567890abcdefghijklmnopqrst",
            "sk_" + "test_1234567890abcdefghijklmnopqrst",
            # GitHub tokens
            "gh" + "p_" + "a" * 36,
            "github_" + "pat_1234567890abcdefghijklmnopqrst",
            # Slack tokens
            "xox" + "b-1234567890-abcdef",
            # Credential assignments (including JSON with closing quote)
            "API_KEY=abcd1234abcd1234abcd",
            'DB_PASSWORD: "correcthorsebatterystaple"',
            '{"API_KEY": "abcd1234abcd1234abcd"}',
        ]
        for text in positives:
            with self.subTest(text=text):
                self.assertIsNotNone(trust.looks_secret(text))
        # Not just Stripe's: the sk- form is also Anthropic's and OpenAI's.
        self.assertEqual(trust.looks_secret("sk-" + "ant-api03-abcdefghijklmnopqrstuvwxyz"), "API key")
        for text in ("the key idea is simple", "TOKEN=short", "use sk-short", "monkey=bananabananabanana"):
            with self.subTest(text=text):
                self.assertIsNone(trust.looks_secret(text))


class VisibilityTest(HomeCase):
    """trust.visibility is the one decision the delivery hook and `read` share (M3/D2)."""

    def setUp(self):
        super().setUp()
        paths.ensure_home()
        self.me_sid, self.peer = new_sid(), new_sid()

    def msg(self, **fields):
        base = {"seq": 1, "id": "a1", "from": "alice", "sid": self.peer, "to": "all",
                "kind": "say", "text": "hi", "mode": "default"}
        base.update(fields)
        return base

    def record_mode(self, sid, mode):
        def set_mode(meta):
            meta["permission_mode"] = mode
        sessions.update_meta(sid, set_mode)

    def verdict(self, msg, receiver_mode="default", inbound="auto", env=None):
        return trust.visibility(msg, self.me_sid, "bob", receiver_mode, inbound, env or {})

    def test_sender_mode_uses_the_stamp_else_the_recorded_mode(self):
        self.assertEqual(trust.sender_mode(self.msg(mode="acceptEdits")), "acceptEdits")
        self.record_mode(self.peer, "bypassPermissions")
        self.assertEqual(trust.sender_mode(self.msg(mode="unknown")), "bypassPermissions")
        msg = self.msg()
        del msg["mode"]
        self.assertEqual(trust.sender_mode(msg), "bypassPermissions")
        self.assertIsNone(trust.sender_mode(self.msg(mode=None, sid="not-a-session-id")))

    def test_addressed(self):
        self.assertTrue(trust.addressed(self.msg(to="all"), "bob"))
        self.assertTrue(trust.addressed(self.msg(to=["carol", "bob"]), "bob"))
        self.assertFalse(trust.addressed(self.msg(to=["carol"]), "bob"))
        self.assertFalse(trust.addressed(self.msg(to=["carol"]), None))
        self.assertFalse(trust.addressed(self.msg(to="bob"), "bob"))

    def test_own_status_and_unaddressed_are_skipped(self):
        self.assertEqual(self.verdict(self.msg(sid=self.me_sid)), ("skip", None))
        self.assertEqual(self.verdict(self.msg(kind="status")), ("skip", None))
        self.assertEqual(self.verdict(self.msg(to=["carol"])), ("skip", None))
        # Skips win over holds: a refused room still never shows the receiver its own lines.
        self.assertEqual(self.verdict(self.msg(sid=self.me_sid), inbound="refuse"), ("skip", None))

    def test_deliver_hold_and_refuse(self):
        self.assertEqual(self.verdict(self.msg(to=["bob"])), ("deliver", None))
        self.assertEqual(self.verdict(self.msg(mode="bypassPermissions")), ("hold", "permission-mode mismatch"))
        self.assertEqual(self.verdict(self.msg(), receiver_mode="auto"), ("hold", "permission-mode mismatch"))
        self.assertEqual(self.verdict(self.msg(), inbound="hold"), ("hold", "inbound=hold"))
        self.assertEqual(self.verdict(self.msg(), inbound="REFUSE"), ("hold", "refuse"))
        allow = {"PASSNOTE_ALLOW_BYPASS": "1"}
        self.assertEqual(self.verdict(self.msg(mode="bypassPermissions"), env=allow), ("deliver", None))
        self.assertEqual(self.verdict(self.msg(), inbound="refuse", env=allow), ("hold", "refuse"))

    def test_unknown_receiver_mode_fails_closed(self):
        for receiver in (None, ""):
            with self.subTest(receiver=receiver):
                self.assertEqual(self.verdict(self.msg(), receiver_mode=receiver),
                                 ("hold", "receiver mode unknown"))
                self.assertEqual(self.verdict(self.msg(mode="auto"), receiver_mode=receiver),
                                 ("hold", "receiver mode unknown"))
        self.assertEqual(self.verdict(self.msg(), receiver_mode=None, inbound="refuse"), ("hold", "refuse"))
        self.assertEqual(self.verdict(self.msg(), receiver_mode=None, env={"PASSNOTE_ALLOW_BYPASS": "1"}),
                         ("deliver", None))


class OwnTest(unittest.TestCase):
    def test_own_matches_my_current_and_earlier_session_ids(self):
        members = {"S-NEW": {"name": "alice", "alias": "a", "prev_sids": ["S-OLD"]}, "S-B": {"name": "bob", "alias": "b"}}
        self.assertTrue(trust.own({"sid": "S-NEW"}, "S-NEW", members))
        self.assertTrue(trust.own({"sid": "S-OLD"}, "S-NEW", members))
        self.assertFalse(trust.own({"sid": "S-B"}, "S-NEW", members))
        self.assertFalse(trust.own({"sid": "S-OLD"}, "S-NEW", None))  # no members: only the current sid
        self.assertTrue(trust.own({"sid": "S-NEW"}, "S-NEW", None))

    def test_a_prev_sid_naming_a_current_member_is_not_mine(self):
        members = {"S-NEW": {"name": "alice", "alias": "a", "prev_sids": ["S-B"]}, "S-B": {"name": "bob", "alias": "b"}}
        self.assertFalse(trust.own({"sid": "S-B"}, "S-NEW", members))


if __name__ == "__main__":
    unittest.main()
