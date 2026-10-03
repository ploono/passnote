import json
import os
import unittest
from unittest import mock

from support import HomeCase, new_sid
from passnote import cursor, paths, store


def append(text, room="r"):
    """Post a valid message (only valid messages are cursor positions)."""
    rec = {"from": "alice", "sid": "s-alice", "to": "all", "kind": "say", "text": text, "mode": "default"}
    return store.append_message(room, rec, "a")


class CursorTest(HomeCase):
    def setUp(self):
        super().setUp()
        paths.ensure_home()
        self.sid = new_sid()

    def test_at_eof_and_round_trip(self):
        append("one")
        cur = cursor.at_eof("r")
        self.assertEqual(cur["off"], os.path.getsize(store.log_path("r")))
        self.assertEqual(cur["seq"], 1)
        self.assertTrue(cursor.save(self.sid, "r", cur))
        self.assertEqual(cursor.load(self.sid, "r"), cur)

    def test_at_eof_creates_missing_log(self):
        cur = cursor.at_eof("fresh")
        self.assertEqual((cur["off"], cur["seq"]), (0, 0))
        self.assertTrue(os.path.exists(store.log_path("fresh")))

    def test_at_eof_stops_after_the_last_line_seq_before_can_verify(self):
        # A join after a malformed or unterminated last line must not save a cursor that
        # cursor.start resets on every fire (which would redeliver the whole log).
        append("one")
        path = store.log_path("r")
        verified_end = os.path.getsize(path)
        for tail in (b'{"seq": "x"}\n', b"not json\n", b'{"seq": 7, "text": "partial'):
            with self.subTest(tail=tail):
                with open(path, "ab") as fh:
                    fh.write(tail)
                cur = cursor.at_eof("r")
                self.assertEqual((cur["off"], cur["seq"]), (verified_end, 1))
                self.assertEqual(cursor.start(cur, os.stat(path), path), (verified_end, 1, False))

    def test_at_eof_scans_back_past_an_unverifiable_tail(self):
        # More than MAX_READ bytes of junk after the last good line: a cursor at 0 would make a
        # new joiner re-read the same window forever, so at_eof looks further back (review 1b).
        for text in ("one", "two"):
            append(text)
        path = store.log_path("r")
        good_end = os.path.getsize(path)
        with mock.patch.object(store, "MAX_READ", 2048):
            for tail in (b"x" * 3000 + b"\n", b"junk\n" * 1000):
                with self.subTest(tail=tail[:8]):
                    with open(path, "ab") as fh:
                        fh.write(tail)
                    cur = cursor.at_eof("r")
                    self.assertEqual((cur["off"], cur["seq"]), (good_end, 2))
                    self.assertEqual(cursor.start(cur, os.stat(path), path), (good_end, 2, False))

    def test_at_eof_without_a_verifiable_line_in_reach_starts_at_zero_past_every_seq(self):
        append("one")
        path = store.log_path("r")
        with open(path, "ab") as fh:
            fh.write(b"junk\n" * 1000)
        with mock.patch.object(store, "MAX_READ", 1024), mock.patch.object(store, "MAX_SKIP", 2048):
            cur = cursor.at_eof("r")
            self.assertEqual((cur["off"], cur["seq"]), (0, 1))
            self.assertEqual(cursor.start(cur, os.stat(path), path), (0, 1, False))

    def test_never_moves_backwards_on_same_inode(self):
        append("one")
        cur = cursor.at_eof("r")
        cursor.save(self.sid, "r", cur)
        self.assertFalse(cursor.save(self.sid, "r", dict(cur, off=0, seq=0)))
        self.assertEqual(cursor.load(self.sid, "r"), cur)

    def test_start_normal_and_reset_cases(self):
        # Long texts first, so a replaced log is always shorter, even if the filesystem reuses the inode.
        for text in ("one-one-one-one", "two-two-two-two", "three-three-three"):
            append(text)
        path = store.log_path("r")
        cur = cursor.at_eof("r")
        st = os.stat(path)
        self.assertEqual(cursor.start(cur, st, path), (cur["off"], 3, False))
        # The cursor is past the end but the log still holds newer seqs: reset to just past the
        # last line at or below our seq (seq 2), then dedupe by seq.
        after_two = [off + len(line) for off, line in self.lines(path)][1]
        ahead = {"ino": cur["ino"], "off": st.st_size + 100, "seq": 2}
        self.assertEqual(cursor.start(ahead, st, path), (after_two, 2, True))
        # The log was replaced and its seqs restarted, even up to the same number: deliver everything.
        os.unlink(path)
        for text in ("x", "y", "z"):
            append(text)
        self.assertEqual(cursor.start(cur, os.stat(path), path), (0, 0, True))

    def test_same_size_rewrite_is_detected(self):
        for text in ("one", "two", "three"):
            append(text)
        path = store.log_path("r")
        cur = cursor.at_eof("r")
        with open(path, "rb") as fh:
            data = fh.read().replace(b'"seq": 3', b'"seq": 9')
        with open(path, "wb") as fh:  # same inode, same size, different last line
            fh.write(data)
        after_two = [off + len(line) for off, line in self.lines(path)][1]
        self.assertEqual(cursor.start(cur, os.stat(path), path), (after_two, 3, True))

    def test_replaced_copy_resumes_past_the_old_lines(self):
        # A log replaced by a copy with more than a read window of old lines: resuming at 0 would
        # re-read the same window of already-delivered lines on every fire (review fix 2).
        with mock.patch.object(store, "MAX_READ", 2048):
            for i in range(40):
                append(f"old{i} " + "y" * 100)
            path = store.log_path("r")
            cur = cursor.at_eof("r")
            with open(path, "rb") as fh:
                data = fh.read()
            # A new inode with the same content. Written beside the log and renamed over it while
            # the old inode is still in use: unlink-then-create lets Linux reuse the inode number.
            with open(path + ".tmp", "wb") as fh:
                fh.write(data)
            os.replace(path + ".tmp", path)
            # Nothing new yet: a copy, not a restart (which would redeliver everything).
            self.assertEqual(cursor.start(cur, os.stat(path), path), (len(data), 40, True))
            append("new")
            self.assertEqual(cursor.start(cur, os.stat(path), path), (len(data), 40, True))
            # Our offset no longer ends a line (e.g. a cursor from before a rewrite): scan back
            # from the end for the last line at or below our seq.
            self.assertEqual(cursor.start(dict(cur, off=len(data) - 1), os.stat(path), path), (len(data), 40, True))
            # No line at or below our seq in reach: back to 0, deduping by our seq.
            self.assertEqual(cursor.start(dict(cur, seq=0, off=1), os.stat(path), path), (0, 0, True))

    def test_last_position_parses_only_lines_that_name_a_seq(self):
        # A window of forged junk lines (no "seq" key) costs a byte search each, not a JSON parse:
        # deeply nested ones are slow to parse. A line hiding its seq behind \u escapes is never a
        # resume point (forged only; resuming earlier just redelivers).
        with mock.patch.object(store, "MAX_SKIP", 1024 * 1024):
            path = store.log_path("r")
            junk = b'{"x": ' + b"[" * 900 + b"]" * 900 + b"}\n"
            append("one")
            with open(path, "ab") as fh:
                fh.write(junk * 250)
            append("two")
            with open(path, "ab") as fh:
                fh.write(junk * 250 + b'{"\\u0073eq": 9}\n')
            size = os.path.getsize(path)
            self.assertLess(size, store.MAX_SKIP)
            after_two = [off + len(line) for off, line in self.lines(path) if b'"two"' in line][0]
            with mock.patch.object(store, "parse", wraps=store.parse) as parse:
                self.assertEqual(cursor._last_position(path, size), (after_two, 2))
            self.assertLessEqual(parse.call_count, 3)  # "one", "two" and seq_before's read-back

    def test_out_of_order_line_is_never_a_resume_point(self):
        for i in range(5):
            append(f"m{i}")
        path = store.log_path("r")
        in_order_end = os.path.getsize(path)
        with open(path, "ab") as fh:
            fh.write(b'{"seq": 2, "text": "forged"}\n')
        self.assertEqual(cursor.at_eof("r"), {"ino": os.stat(path).st_ino, "off": in_order_end, "seq": 5})
        stale = {"ino": os.stat(path).st_ino + 1, "off": 1, "seq": 4}
        after_four = [off + len(line) for off, line in self.lines(path)][3]
        self.assertEqual(cursor.start(stale, os.stat(path), path), (after_four, 4, True))

    def test_forged_tail_lines_are_never_a_resume_point(self):
        # The hook's own rule: a cursor position ends a valid message whose seq is not below any
        # valid line before it. Descending-then-ascending forged lines, whole messages or bare
        # {"seq": N}, must neither be at_eof's position nor start()'s resume point (review fix 4).
        def forged(seq):
            return json.dumps({"v": 1, "seq": seq, "id": f"z{seq}", "from": "mallory", "sid": "s-m",
                               "to": "all", "kind": "say", "text": "forged"}).encode("ascii")

        tails = [
            ("messages-5-6", [forged(5), forged(6)]),
            ("bare-5-6", [b'{"seq": 5}', b'{"seq": 6}']),
            ("messages-7-3-4-5", [forged(7), forged(3), forged(4), forged(5)]),
            ("bare-and-messages", [b'{"seq": 3}', forged(4), b'{"seq": 5}', forged(6)]),
        ]
        for room, tail in tails:
            with self.subTest(tail=room):
                for i in range(50):
                    append(f"m{i}", room)
                path = store.log_path(room)
                ends = [off + len(line) for off, line in self.lines(path)]
                with open(path, "ab") as fh:
                    fh.write(b"".join(line + b"\n" for line in tail))
                st = os.stat(path)
                self.assertEqual(cursor.at_eof(room), {"ino": st.st_ino, "off": ends[49], "seq": 50})
                stale = {"ino": st.st_ino + 1, "off": 1, "seq": 40}
                self.assertEqual(cursor.start(stale, st, path), (ends[39], 40, True))
                # Seen through a small read window too (the forged lines fill it, or nearly).
                with mock.patch.object(store, "MAX_READ", 400):
                    self.assertEqual(cursor.at_eof(room)["off"], ends[49])
                    self.assertEqual(cursor.start(stale, st, path), (ends[39], 40, True))

    def test_first_valid_line_of_a_partial_window_is_never_a_position_alone(self):
        # The scan window starts inside a long line: the forged {seq 5} after it may follow
        # seq 30, so with no valid line before it in the window it can't be shown in order.
        for i in range(30):
            append(f"m{i}")
        path = store.log_path("r")
        forged = {"v": 1, "seq": 5, "id": "z5", "from": "mallory", "sid": "s-m", "to": "all",
                  "kind": "say", "text": "forged"}
        with open(path, "ab") as fh:
            fh.write(b"x" * 3000 + b"\n" + json.dumps(forged).encode("ascii") + b"\n" + b"junk\n" * 200)
        st = os.stat(path)
        with mock.patch.object(store, "MAX_READ", 1024), mock.patch.object(store, "MAX_SKIP", 2048):
            self.assertEqual(cursor.at_eof("r"), {"ino": st.st_ino, "off": 0, "seq": 30})
            self.assertEqual(cursor.start({"ino": st.st_ino + 1, "off": 1, "seq": 20}, st, path), (0, 20, True))

    def test_a_line_that_is_not_a_valid_message_does_not_verify_a_cursor(self):
        append("one")
        path = store.log_path("r")
        good_end = os.path.getsize(path)
        with open(path, "ab") as fh:
            fh.write(b'{"seq": 2}\n')
        bare = {"ino": os.stat(path).st_ino, "off": os.path.getsize(path), "seq": 2}
        append("three")
        st = os.stat(path)
        # Not the same file (nor a copy) at a bare line's end: resume just past the last valid
        # line at or below the cursor's seq instead.
        self.assertEqual(cursor.start(bare, st, path), (good_end, 2, True))
        self.assertEqual(cursor.start(dict(bare, ino=st.st_ino + 1), st, path), (good_end, 2, True))

    @staticmethod
    def lines(path):
        """[(offset, line with its newline)] of a log."""
        out, pos = [], 0
        with open(path, "rb") as fh:
            for line in fh:
                out.append((pos, line))
                pos += len(line)
        return out

    def test_cursor_after_a_large_legitimate_line_is_not_reset(self):
        # 4,000 emoji (within the post cap) serialize to ~48 KB: more than seq_before's first
        # read-back window. A cursor ending at that line must still count as the same file.
        append("one")
        append("\U0001f44b" * 4000)
        path = store.log_path("r")
        self.assertGreater(os.path.getsize(path), 48000)
        cur = cursor.at_eof("r")
        for fire in range(2):
            with self.subTest(fire=fire):
                self.assertEqual(cursor.start(cur, os.stat(path), path), (cur["off"], 2, False))
                cursor.save(self.sid, "r", cur)
                cur = cursor.load(self.sid, "r")

    def test_reset_save_moves_the_seq_back_only_for_a_restarted_log(self):
        append("one")
        append("two")
        cur = cursor.at_eof("r")
        cursor.save(self.sid, "r", cur)
        # A replaced log may put our line at a lower offset, but our seq stays (spec §7 dedupe).
        self.assertTrue(cursor.save(self.sid, "r", dict(cur, ino=cur["ino"] + 1, off=1), reset=True))
        self.assertFalse(cursor.save(self.sid, "r", dict(cur, off=1, seq=1), reset=True))
        self.assertEqual(cursor.load(self.sid, "r"), dict(cur, ino=cur["ino"] + 1, off=1))
        # A restarted log (its newest seq not beyond ours) redelivers from 0.
        self.assertTrue(cursor.save(self.sid, "r", dict(cur, off=0, seq=0), reset=True, restarted=True))
        self.assertEqual(cursor.load(self.sid, "r"), dict(cur, off=0, seq=0))

    def test_load_rejects_malformed(self):
        paths.atomic_write_json(cursor.path(self.sid, "r"), {"ino": "x"})
        self.assertIsNone(cursor.load(self.sid, "r"))
        cursor.remove(self.sid, "r")
        self.assertFalse(os.path.exists(cursor.path(self.sid, "r")))

    def test_bad_room_name_raises_error(self):
        for bad_room in ("../x", ""):
            with self.subTest(room=bad_room):
                with self.assertRaises(paths.PassnoteError):
                    cursor.load(self.sid, bad_room)
                with self.assertRaises(paths.PassnoteError):
                    cursor.save(self.sid, bad_room, {"ino": 1, "off": 0, "seq": 0})
                with self.assertRaises(paths.PassnoteError):
                    cursor.remove(self.sid, bad_room)


if __name__ == "__main__":
    unittest.main()
