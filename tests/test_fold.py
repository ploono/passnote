import unittest

import support  # noqa: F401

from passnote import fold

MEMBERS = {"SA": {"name": "alice"}, "SB": {"name": "bob"}, "SC": {"name": "carol"}}


def m(seq, sender, kind, text="t", to="all", re=None):
    sid = {"alice": "SA", "bob": "SB", "carol": "SC", "dave": "SD"}[sender]
    out = {"id": f"{sender[0]}{seq}", "seq": seq, "from": sender, "sid": sid, "to": to, "kind": kind, "text": text}
    if re:
        out["re"] = re
    return out


class FoldTest(unittest.TestCase):
    def test_unanswered_until_each_addressee_replies(self):
        msgs = [m(1, "alice", "ask", to=["bob", "carol"]), m(2, "bob", "ans", re="a1", to=["alice"]),
                m(3, "alice", "ask", to="all"), m(4, "carol", "err", to=["bob"])]
        state = fold.fold(msgs, MEMBERS, {})
        unanswered = {p["id"]: p["waiting_on"] for p in state["unanswered"]}
        self.assertEqual(unanswered, {"a1": ["carol"], "c4": ["bob"]})

    def test_claims_released_or_departed(self):
        msgs = [m(1, "alice", "claim", "refactor api"), m(2, "bob", "claim", "docs"),
                m(3, "bob", "claim", "release", re="b2"), m(4, "dave", "claim", "gone")]
        state = fold.fold(msgs, MEMBERS, {})
        self.assertEqual([c["id"] for c in state["claims"]], ["a1"])

    def test_an_earlier_session_id_still_counts_after_clear(self):
        # /clear gave alice a new session id (SA2) and she was renamed alicia since: her claim,
        # status and reply from SA are still hers.
        members = {"SA2": {"name": "alicia", "prev_sids": ["SA"]}, "SB": {"name": "bob"}}
        msgs = [m(1, "alice", "claim", "migration"), m(2, "alice", "status", "busy"),
                m(3, "bob", "ask", to=["alicia"]), m(4, "alice", "ans", re="b3", to=["bob"])]
        state = fold.fold(msgs, members, {})
        self.assertEqual([c["id"] for c in state["claims"]], ["a1"])
        self.assertEqual(state["status"], {"alicia": "busy"})
        self.assertEqual(state["unanswered"], [])

    def test_status_latest_wins(self):
        msgs = [m(1, "alice", "status", "reading"), m(2, "alice", "status", "testing")]
        self.assertEqual(fold.fold(msgs, MEMBERS, {})["status"], {"alice": "testing"})

    def test_props_seen_by_cursor(self):
        msgs = [m(5, "alice", "prop", "ship at 3pm", to=["bob", "carol"])]
        cursors = {"SB": {"ino": 1, "off": 10, "seq": 5}, "SC": {"ino": 1, "off": 1, "seq": 4}}
        prop = fold.fold(msgs, MEMBERS, cursors)["props"][0]
        self.assertEqual((prop["seen"], prop["unseen"]), (["bob"], ["carol"]))

    def test_a_held_prop_is_not_seen_by_its_addressee(self):
        msgs = [m(5, "alice", "prop", "ship at 3pm", to=["bob", "carol"])]
        cursors = {"SB": {"ino": 1, "off": 10, "seq": 5}, "SC": {"ino": 1, "off": 10, "seq": 5}}
        prop = fold.fold(msgs, MEMBERS, cursors, held={("a5", "SC"), ("zz", "SB")})["props"][0]
        self.assertEqual((prop["seen"], prop["unseen"]), (["bob"], ["carol"]))
        prop = fold.fold(msgs, MEMBERS, cursors, held=None)["props"][0]
        self.assertEqual((prop["seen"], prop["unseen"]), (["bob", "carol"], []))

    def test_malformed_messages_skipped_gracefully(self):
        # Well-formed baseline
        baseline = [m(1, "alice", "ask", to=["bob", "carol"]), m(2, "bob", "ans", re="a1", to=["alice"])]
        baseline_state = fold.fold(baseline, MEMBERS, {})
        # Mix in malformed messages: seq as list, id as list, from as list, to with nested list, sid as list, etc.
        malformed = baseline + [
            {"seq": "not-int", "id": "x1", "from": "dave", "sid": "SD", "kind": "say", "text": "bad"},
            {"seq": 3, "id": ["x"], "from": "dave", "sid": "SD", "kind": "say", "text": "bad"},
            {"seq": 3, "id": "x3", "from": ["dave"], "sid": "SD", "kind": "ask", "text": "bad", "to": ["bob"]},
            {"seq": 3, "id": "x3", "from": "dave", "sid": ["SD"], "kind": "say", "text": "bad"},
            {"seq": 3, "id": "x4", "from": "dave", "sid": "SD", "kind": "ask", "text": "bad", "to": [["bob"]]},
            {"seq": None, "id": "x5", "from": "dave", "sid": "SD", "kind": "say", "text": "bad"},
            {"seq": 3, "id": "x6", "from": "dave", "sid": "SD", "kind": "say", "text": "bad", "ts": True},
            # Each of these would answer a1 for carol, change claims/status or mark a prop seen.
            {"seq": 3, "id": "c7", "from": "carol", "sid": "SC", "kind": "ans", "text": "bad", "to": ["alice"], "re": ["a1"]},
            {"seq": 3, "id": "c8", "from": ["carol"], "sid": "SC", "kind": "ans", "text": "bad", "to": ["alice"], "re": "a1"},
            {"seq": 3, "id": "c9", "from": "carol", "sid": "SC", "kind": "ans", "text": "bad", "re": "a1"},
            {"seq": 3, "id": "b10", "from": "bob", "sid": ["SB"], "kind": "claim", "text": "bad", "to": "all"},
            {"seq": 3, "id": "b11", "from": "bob", "sid": ["SB"], "kind": "status", "text": "bad", "to": "all"},
            {"seq": "5", "id": "a12", "from": "alice", "sid": "SA", "kind": "prop", "text": "bad", "to": ["bob"]},
        ]
        # Fold with malformed messages should not raise
        malformed_state = fold.fold(malformed, MEMBERS, {})
        # Results for well-formed messages should be identical
        self.assertEqual(baseline_state["unanswered"], malformed_state["unanswered"])
        self.assertEqual(baseline_state["claims"], malformed_state["claims"])
        self.assertEqual(baseline_state["status"], malformed_state["status"])
        self.assertEqual(baseline_state["props"], malformed_state["props"])

    def test_prop_without_seq_not_seen(self):
        # Prop with missing seq should be skipped entirely (not reported as seen)
        msgs = [{"seq": None, "id": "a1", "from": "alice", "sid": "SA", "to": ["bob"], "kind": "prop", "text": "test"}]
        cursors = {"SB": {"ino": 1, "off": 10, "seq": 10}}
        state = fold.fold(msgs, MEMBERS, cursors)
        self.assertEqual(state["props"], [])

    def test_non_dict_member_values_skipped(self):
        # Members with non-dict values should be skipped
        bad_members = {"SA": {"name": "alice"}, "SB": "not-a-dict", "SC": {"name": "carol"}}
        msgs = [m(1, "alice", "ask", to=["bob", "carol"])]
        state = fold.fold(msgs, bad_members, {})
        # Should process normally, but ignore the non-dict member
        self.assertEqual(len(state["unanswered"]), 1)

    def test_members_without_a_string_name_are_skipped(self):
        members = {"SA": {"name": "alice"}, "SB": {"name": ["bob"]}, "SC": {"name": "carol"}, "SD": {}}
        msgs = [m(1, "alice", "ask", to=["bob", "carol"]), m(2, "carol", "ans", re="a1", to=["alice"]),
                m(3, "bob", "claim", "docs")]
        state = fold.fold(msgs, members, {})
        self.assertEqual([(u["id"], u["waiting_on"]) for u in state["unanswered"]], [("a1", ["bob"])])
        self.assertEqual(state["claims"], [])

    def test_replied_uses_member_name(self):
        # The replied tracking should use the member name from members, not the from field
        # This prevents a forged from field from clearing an unanswered ask
        msgs = [
            m(1, "alice", "ask", to=["bob"]),
            # This message has sid "SB" (bob) but from "eve" (forged)
            {"seq": 2, "id": "e2", "from": "eve", "sid": "SB", "kind": "ans", "text": "yes", "to": ["alice"], "re": "a1"}
        ]
        state = fold.fold(msgs, MEMBERS, {})
        # Even though the from field says "eve", the sid says it's from bob, so alice's ask should be marked answered
        self.assertEqual(state["unanswered"], [])


if __name__ == "__main__":
    unittest.main()
