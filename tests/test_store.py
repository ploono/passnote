import json
import multiprocessing
import os
import unittest
from unittest import mock

from support import HomeCase, new_sid
from passnote import paths, store


def _post_many(home, room, alias, count):
    os.environ["PASSNOTE_HOME"] = home
    for i in range(count):
        store.append_message(room, {"from": alias, "sid": "x", "to": "all", "kind": "say", "text": f"{alias}-{i}"}, alias)


class _CountingFile:
    """Wraps a file object and counts the bytes its read() returns."""

    def __init__(self, fh, counter):
        self._fh, self._counter = fh, counter

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()

    def read(self, n=-1):
        data = self._fh.read(n)
        self._counter.append(len(data))
        return data

    def __getattr__(self, name):
        return getattr(self._fh, name)


def counting_open(counter):
    return lambda *args, **kwargs: _CountingFile(open(*args, **kwargs), counter)


def _reap(proc):
    """Never leave a child running past its test (a timed-out join would otherwise orphan it)."""
    if proc.is_alive():
        proc.kill()
        proc.join(5)


class StoreTest(HomeCase):
    def setUp(self):
        super().setUp()
        paths.ensure_home()

    def test_append_assigns_seq_and_id(self):
        first = store.append_message("r", {"text": "one"}, "a")
        second = store.append_message("r", {"text": "two"}, "bo")
        self.assertEqual((first["seq"], first["id"], first["v"]), (1, "a1", 1))
        self.assertEqual((second["seq"], second["id"]), (2, "bo2"))
        self.assertEqual([m["text"] for _, m in store.iter_messages("r")], ["one", "two"])

    def test_fragment_is_repaired_before_append(self):
        store.append_message("r", {"text": "one"}, "a")
        with open(store.log_path("r"), "ab") as fh:
            fh.write(b'{"partial": ')
        rec = store.append_message("r", {"text": "two"}, "a")
        self.assertEqual(rec["seq"], 2)
        texts = [m.get("text") for _, m in store.iter_messages("r")]
        self.assertEqual(texts, ["one", "two"])

    def test_read_from_returns_complete_lines_only(self):
        store.append_message("r", {"text": "one"}, "a")
        path = store.log_path("r")
        with open(path, "ab") as fh:
            fh.write(b'{"seq": 99')
        lines, end = store.read_from(path, 0)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0][0], 0)
        self.assertEqual(end, len(lines[0][1]) + 1)
        self.assertEqual(store.read_at(path, 0, len(lines[0][1])), lines[0][1])

    def test_seq_before(self):
        rec = {"from": "alice", "sid": "s-alice", "to": "all", "kind": "say", "mode": "default"}
        store.append_message("r", dict(rec, text="one"), "a")
        store.append_message("r", dict(rec, text="two"), "a")
        path = store.log_path("r")
        self.assertEqual(store.seq_before(path, os.path.getsize(path)), 2)
        lines, _ = store.read_from(path, 0)
        self.assertEqual(store.seq_before(path, lines[1][0]), 1)
        self.assertIsNone(store.seq_before(path, 0))
        self.assertIsNone(store.seq_before(path, 5))
        # Only a valid message is a cursor position: a bare or malformed line with a seq is not.
        for line in (b'{"seq": 3}', b'{"seq": 3, "text": "no sender"}'):
            with open(path, "ab") as fh:
                fh.write(line + b"\n")
            self.assertIsNone(store.seq_before(path, os.path.getsize(path)))

    def test_last_seq_scans_past_large_tail(self):
        for i in range(5):
            store.append_message("r", {"text": "x" * 3000}, "a")
        self.assertEqual(store.last_seq(store.log_path("r")), 5)
        self.assertEqual(store.last_seq(os.path.join(self.home, "nope.jsonl")), 0)

    def test_non_ascii_round_trip(self):
        text = "héllo 👋 世界 שלום"
        store.append_message("r", {"text": text}, "a")
        with open(store.log_path("r"), "rb") as fh:
            raw = fh.read()
        self.assertTrue(all(b < 128 for b in raw))
        self.assertEqual(store.iter_messages("r")[0][1]["text"], text)

    def test_concurrent_appends_keep_unique_increasing_seq(self):
        # Pin "spawn": the platform default differs (fork on Linux <= 3.13, forkserver on 3.14+).
        ctx = multiprocessing.get_context("spawn")
        procs = [ctx.Process(target=_post_many, args=(self.home, "r", alias, 1000)) for alias in "abcd"]
        for proc in procs:
            proc.start()
            self.addCleanup(_reap, proc)
        for proc in procs:
            proc.join(120)
            self.assertEqual(proc.exitcode, 0)
        msgs = [m for _, m in store.iter_messages("r")]
        self.assertEqual(len(msgs), 4000)
        self.assertEqual([m["seq"] for m in msgs], list(range(1, 4001)))
        with open(store.log_path("r"), "rb") as fh:
            for raw in fh.read().split(b"\n")[:-1]:
                json.loads(raw)

    # -- a single poison line must never block a room (S1, S2, S11) ---------

    def test_deeply_nested_line_blocks_neither_delivery_nor_posting(self):
        store.append_message("r", {"text": "one"}, "a")
        with open(store.log_path("r"), "ab") as fh:
            fh.write(b"[" * 200000 + b"]" * 200000 + b"\n")
        self.assertIsNone(store.parse(b"[" * 200000 + b"]" * 200000))
        self.assertEqual(store.last_seq(store.log_path("r")), 1)
        self.assertEqual(store.append_message("r", {"text": "two"}, "a")["seq"], 2)
        self.assertEqual([m["text"] for _, m in store.iter_messages("r")], ["one", "two"])

    def test_read_from_skips_a_line_longer_than_the_read_window(self):
        store.append_message("r", {"text": "one"}, "a")
        path = store.log_path("r")
        with open(path, "ab") as fh:
            fh.write(b'{"junk": "' + b"x" * 500 + b'"}\n')
            fh.write(b'{"junk": "' + b"z" * 300 + b'"}\n')
        store.append_message("r", {"text": "after"}, "a")
        lines, end = store.read_from(path, 0, 200)
        self.assertEqual([store.parse(raw)["text"] for _, raw in lines], ["one"])
        # The next window holds no newline: skip the oversized lines and return what follows,
        # so the end offset lands after a whole line the cursor can be checked against.
        lines, end = store.read_from(path, end, 200)
        self.assertEqual([store.parse(raw)["text"] for _, raw in lines], ["after"])
        self.assertEqual(end, os.path.getsize(path))
        self.assertEqual(store.read_at(path, lines[0][0], len(lines[0][1])), lines[0][1])

    def test_read_from_waits_while_nothing_whole_follows_an_oversized_line(self):
        store.append_message("r", {"text": "one"}, "a")
        path = store.log_path("r")
        _, end = store.read_from(path, 0)
        with open(path, "ab") as fh:
            fh.write(b"x" * 500)  # no newline yet: maybe a write in progress
        self.assertEqual(store.read_from(path, end, 200), ([], end))
        with open(path, "ab") as fh:
            fh.write(b"\n")  # oversized and complete, but the last line: nothing to deliver yet
        self.assertEqual(store.read_from(path, end, 200), ([], end))
        store.append_message("r", {"text": "after"}, "a")
        lines, _ = store.read_from(path, end, 200)
        self.assertEqual([store.parse(raw)["text"] for _, raw in lines], ["after"])

    def test_read_from_skips_an_oversized_line_at_full_read_cap(self):
        path = store.log_path("r")
        paths.makedirs(os.path.dirname(path))
        with open(path, "wb") as fh:
            fh.write(b'{"junk": "' + b"x" * (store.MAX_READ + 10) + b'"}\n')
        store.append_message("r", {"text": "after"}, "a")
        lines, end = store.read_from(path, 0)
        self.assertEqual([store.parse(raw)["text"] for _, raw in lines], ["after"])
        self.assertEqual(end, os.path.getsize(path))

    def test_seq_before_reads_back_a_line_up_to_the_read_cap(self):
        rec = {"from": "alice", "sid": "s-alice", "to": "all", "kind": "say", "mode": "default"}
        store.append_message("r", dict(rec, text="x" * (store.MAX_READ - 300)), "a")
        path = store.log_path("r")
        self.assertGreater(os.path.getsize(path), store.MAX_READ - 200)
        self.assertEqual(store.seq_before(path, os.path.getsize(path)), 1)
        lines, end = store.read_from(path, 0)
        self.assertEqual((len(lines), end), (1, os.path.getsize(path)))

    def test_read_from_gives_up_on_a_line_past_the_scan_cap(self):
        # A multi-GB forged line must not push every fire past the hook timeout: past the cap,
        # stay put (and log it once).
        store.append_message("r", {"text": "one"}, "a")
        path = store.log_path("r")
        _, end = store.read_from(path, 0)
        with open(path, "ab") as fh:
            fh.write(b'{"junk": "' + b"x" * 5000 + b'"}\n')
        store.append_message("r", {"text": "after"}, "a")
        with mock.patch.object(store, "MAX_SKIP", 1000):
            self.assertEqual(store.read_from(path, end, 200), ([], end))
            self.assertEqual(store.read_from(path, end, 200), ([], end))
        with open(os.path.join(self.home, "errors.log")) as fh:
            logged = [json.loads(line) for line in fh]
        self.assertEqual([rec["where"] for rec in logged], ["read_from"])
        lines, _ = store.read_from(path, end, 200)  # under the default cap it is skipped
        self.assertEqual([store.parse(raw)["text"] for _, raw in lines], ["after"])

    def _oversized_run(self, count, width=200):
        """A log of one message, then `count` lines of width+1 bytes (each one byte past a
        width-byte read window), then a valid message. Returns (path, offset after the first)."""
        store.append_message("r", {"text": "one"}, "a")
        path = store.log_path("r")
        _, end = store.read_from(path, 0)
        with open(path, "ab") as fh:
            for _ in range(count):
                fh.write(b"x" * width + b"\n")
        store.append_message("r", {"text": "after"}, "a")
        return path, end

    def _logged(self):
        try:
            with open(os.path.join(self.home, "errors.log")) as fh:
                return [json.loads(line)["where"] for line in fh]
        except FileNotFoundError:
            return []

    def test_read_from_bounds_the_bytes_read_per_call(self):
        # Reviewer probe: each line one byte past the window used to cost a full window plus a
        # 64 KiB scan while being charged ~1 byte, so a long run was reread in full every fire.
        path, end = self._oversized_run(200)
        read = []
        with mock.patch.object(store, "MAX_SKIP", 1000), \
                mock.patch.object(store, "open", counting_open(read), create=True):
            for _ in range(2):
                del read[:]
                self.assertEqual(store.read_from(path, end, 200), ([], end))
                self.assertLessEqual(sum(read), 1000)
        self.assertEqual(self._logged(), ["read_from"])  # logged once, not per call

    def test_read_from_delivers_after_a_run_that_fits_the_bound(self):
        path, end = self._oversized_run(3)
        read = []
        with mock.patch.object(store, "MAX_SKIP", 1000), \
                mock.patch.object(store, "open", counting_open(read), create=True):
            lines, new_end = store.read_from(path, end, 200)
        self.assertEqual([store.parse(raw)["text"] for _, raw in lines], ["after"])
        self.assertEqual(new_end, os.path.getsize(path))
        self.assertLessEqual(sum(read), 1000)
        self.assertEqual(self._logged(), [])

    def test_a_run_longer_than_the_bound_stalls_at_the_same_offset(self):
        # Documented behaviour: past the bound there's no safe place to resume, so delivery
        # stays at `off` (logged once) rather than landing the cursor after an oversized line.
        path, end = self._oversized_run(20)
        with mock.patch.object(store, "MAX_SKIP", 1000):
            self.assertEqual(store.read_from(path, end, 200), ([], end))
            store.append_message("r", {"text": "later"}, "a")
            self.assertEqual(store.read_from(path, end, 200), ([], end))
        self.assertEqual(self._logged(), ["read_from"])
        # Under the real bound the run is skipped and both messages arrive.
        lines, _ = store.read_from(path, end, 200)
        self.assertEqual([store.parse(raw)["text"] for _, raw in lines], ["after", "later"])

    def test_a_replaced_log_is_logged_again(self):
        path, end = self._oversized_run(20)
        with mock.patch.object(store, "MAX_SKIP", 1000):
            store.read_from(path, end, 200)
            with open(path, "rb") as fh:
                data = fh.read()
            with open(path + ".new", "wb") as fh:
                fh.write(data)
            os.replace(path + ".new", path)  # a different inode, same offsets
            store.read_from(path, end, 200)
        self.assertEqual(self._logged(), ["read_from", "read_from"])

    def test_scanning_for_a_newline_does_not_read_far_past_it(self):
        path, end = self._oversized_run(1, width=200)
        for i in range(200):  # plenty after the newline that an over-eager scan would read
            store.append_message("r", {"text": f"m{i}"}, "a")
        read = []
        with mock.patch.object(store, "open", counting_open(read), create=True):
            lines, _ = store.read_from(path, end, 200)
        self.assertEqual(store.parse(lines[0][1])["text"], "after")
        # one window, a short scan to the newline, one window for what follows
        self.assertLessEqual(sum(read), 200 + 4096 + 200)

    def test_short_write_is_a_failed_post(self):
        real_write = os.write

        def short(fd, data):
            return real_write(fd, data[:-1])

        with mock.patch.object(store.os, "write", short):
            with self.assertRaises(OSError):
                store.append_message("r", {"text": "one"}, "a")
            with self.assertRaises(OSError):
                store.append_event("r", {"type": "join"})

    def test_load_members_drops_malformed_entries(self):
        good = new_sid()
        paths.atomic_write_json(store.members_path("r"), {
            "s1": "not-a-dict",
            "s2": {"name": 5, "alias": "b"},
            "s3": {"name": "carol"},
            good: {"name": "alice", "alias": "a", "joined_at": 1, "root": "/x"},
        })
        self.assertEqual(list(store.load_members("r")), [good])

    def test_load_members_keeps_only_valid_earlier_session_ids(self):
        sid, old, older = new_sid(), new_sid(), new_sid()
        earlier = [new_sid() for _ in range(20)]
        paths.atomic_write_json(store.members_path("r"), {
            sid: {"name": "alice", "alias": "a", "prev_sids": [older, "../x", 5, None, old]},
            "s2": {"name": "bob", "alias": "b", "prev_sids": "not-a-list"},
            "s3": {"name": "carol", "alias": "c", "prev_sids": earlier},
        })
        members = store.load_members("r")
        self.assertEqual(members[sid]["prev_sids"], [older, old])
        self.assertNotIn("prev_sids", members["s2"])
        self.assertEqual(members["s2"]["name"], "bob")
        self.assertEqual(members["s3"]["prev_sids"], earlier[-16:])

    def test_member_for_sid_matches_a_current_or_an_earlier_session_id(self):
        members = {"S-A": {"name": "alice", "prev_sids": ["S-OLD"]}, "S-B": {"name": "bob"}, "S-C": "junk"}
        self.assertEqual(store.member_for_sid(members, "S-A"), ("S-A", members["S-A"]))
        self.assertEqual(store.member_for_sid(members, "S-OLD"), ("S-A", members["S-A"]))
        self.assertIsNone(store.member_for_sid(members, "S-X"))
        self.assertIsNone(store.member_for_sid(members, None))
        self.assertIsNone(store.member_for_sid(None, "S-A"))
        # members.json is writable by any member: an earlier id two members claim belongs to neither.
        members["S-B"]["prev_sids"] = ["S-OLD"]
        self.assertIsNone(store.member_for_sid(members, "S-OLD"))
        # A current id is never another member's earlier one.
        members["S-B"]["prev_sids"] = ["S-A"]
        self.assertEqual(store.member_for_sid(members, "S-A")[0], "S-A")

    def test_iter_messages_offsets_match_the_log(self):
        store.append_message("r", {"text": "one"}, "a")
        path = store.log_path("r")
        with open(path, "ab") as fh:
            fh.write(b"not json\n")
        store.append_message("r", {"text": "t\u00e9" * 50}, "a")
        with open(path, "ab") as fh:
            fh.write(b'{"partial": ')
        msgs = store.iter_messages("r")
        self.assertEqual([m["text"] for _, m in msgs], ["one", "t\u00e9" * 50])
        lines, _ = store.read_from(path, 0)
        offsets = [off for off, raw in lines if store.parse(raw)]
        self.assertEqual([off for off, _ in msgs], offsets)
        for off, msg in msgs:
            self.assertEqual(store.parse(store.read_at(path, off, 4096).split(b"\n")[0]), msg)

    def test_events_members_and_meta(self):
        store.append_event("r", {"type": "join", "sid": "s"})
        self.assertEqual(store.read_events("r")[0]["type"], "join")
        sid = new_sid()
        store.save_members("r", {sid: {"name": "n", "alias": "n", "joined_at": 1, "root": "/x"}})
        self.assertEqual(store.load_members("r")[sid]["name"], "n")
        store.save_meta("r", {"root": "/x"})
        self.assertEqual(store.load_meta("r"), {"root": "/x"})
        self.assertEqual(store.load_members("other"), {})


ABSENT = object()
WELL_FORMED = {"seq": 1, "id": "a1", "from": "alice", "sid": "SA", "kind": "say", "text": "hello", "to": "all"}


def with_field(field, value):
    msg = dict(WELL_FORMED)
    if value is ABSENT:
        msg.pop(field, None)
    else:
        msg[field] = value
    return msg


class ValidMessageTest(HomeCase):
    def test_accepts_well_formed(self):
        self.assertTrue(store.valid_message(WELL_FORMED))

    def test_rejects_non_dict(self):
        for value in (None, "string", [], 1):
            with self.subTest(value=value):
                self.assertFalse(store.valid_message(value))

    def test_rejects_bad_values(self):
        cases = [
            ("seq", ABSENT), ("seq", None), ("seq", "1"), ("seq", True), ("seq", 0), ("seq", -1), ("seq", 1.0),
            ("id", ABSENT), ("id", 1), ("id", ["a1"]),
            ("from", ABSENT), ("from", ["alice"]), ("from", None),
            ("sid", ABSENT), ("sid", ["SA"]),
            ("kind", ABSENT), ("kind", 123),
            ("text", ABSENT), ("text", 123), ("text", None),
            ("to", ABSENT), ("to", None), ("to", "bob"), ("to", ["bob", 123]), ("to", [["bob"]]), ("to", {"bob": 1}),
            ("re", 123), ("re", ["a1"]),
            ("mode", 123), ("mode", ["default"]),
            ("wake", None), ("wake", "yes"), ("wake", 1),
            ("ts", True), ("ts", "1234"),
        ]
        for field, value in cases:
            with self.subTest(field=field, value="<absent>" if value is ABSENT else value):
                self.assertFalse(store.valid_message(with_field(field, value)))

    def test_accepts_good_values(self):
        cases = [
            ("seq", 10 ** 12),
            ("to", "all"), ("to", ["bob"]), ("to", ["bob", "carol"]),
            ("re", ABSENT), ("re", None), ("re", "b1"),
            ("mode", ABSENT), ("mode", None), ("mode", "default"), ("mode", "unknown"), ("mode", "custom"),
            ("wake", ABSENT), ("wake", True), ("wake", False),
            ("ts", ABSENT), ("ts", 1234), ("ts", 1234.5),
            ("full_chars", ABSENT), ("full_chars", None), ("full_chars", 5000),
            # A forged full_chars never hides its line (fails open): render ignores a bad value.
            ("full_chars", "5"), ("full_chars", True), ("full_chars", -1), ("full_chars", 1.5),
            ("full_chars", [1]), ("full_chars", {"n": 1}),
        ]
        for field, value in cases:
            with self.subTest(field=field, value="<absent>" if value is ABSENT else value):
                self.assertTrue(store.valid_message(with_field(field, value)))

    def test_a_real_appended_record_round_trips_as_valid(self):
        paths.ensure_home()
        sid = new_sid()
        rec = store.append_message("r", {"from": "alice", "sid": sid, "to": ["bob"], "kind": "ask",
                                         "text": "split step 2?", "mode": "default", "wake": True}, "a")
        _, msg = store.iter_messages("r")[-1]
        self.assertEqual(msg, rec)
        self.assertTrue(store.valid_message(msg))


class FullTextTest(HomeCase):
    def setUp(self):
        super().setUp()
        paths.ensure_home()
        self.rec = {"from": "alice", "sid": new_sid(), "to": "all", "kind": "say", "text": "x" * 4000, "mode": "default"}

    def test_append_with_full_text_writes_the_file_first(self):
        msg = store.append_message("r", self.rec, "a", full_text="x" * 5000)
        path = store.full_text_path("r", msg["id"])
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "x" * 5000)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o777, 0o700)
        logged = store.iter_messages("r")[-1][1]
        self.assertEqual((len(logged["text"]), logged["full_chars"]), (4000, 5000))

    def test_full_text_file_is_removed_when_the_append_fails(self):
        with mock.patch.object(store, "_write_record", side_effect=OSError(28, "No space left")):
            with self.assertRaises(OSError):
                store.append_message("r", self.rec, "a", full_text="x" * 5000)
        self.assertEqual(os.listdir(store.full_text_dir("r")), [])

    def test_full_text_path_rejects_forged_ids(self):
        for forged in ("../x", "a1/../../etc", "A1", "", None, 7, "a" * 73 + "1", "a1.txt", "a", "a1\n"):
            with self.subTest(forged=forged):
                self.assertIsNone(store.full_text_path("r", forged))
        self.assertTrue(store.full_text_path("r", "b12").endswith(os.path.join("rooms", "r", "files", "b12.txt")))

    def test_every_alias_shape_gets_a_path(self):
        from passnote import rooms
        long_name = "z" * 64
        for name, used in (("Алиса", set()), ("42-._", set()), ("bob", {"b", "bo", "bob"}),
                           (long_name, {long_name[:n] for n in range(1, 65)} | {long_name + "a"})):
            with self.subTest(name=name):
                self.assertIsNotNone(store.full_text_path("r", rooms._alias(name, used) + str(10 ** 17)))

    def test_a_huge_cjk_post_keeps_a_readable_log_line(self):
        msg = store.append_message("r", dict(self.rec, text="漢" * 4000), "a", full_text="漢" * 100000)
        with open(store.log_path("r"), "rb") as fh:
            raw = fh.read().split(b"\n")[0]
        self.assertLess(len(raw), store.MAX_READ)
        self.assertEqual(store.read_from(store.log_path("r"), 0)[0][0][1], raw)
        self.assertEqual(msg["full_chars"], 100000)


if __name__ == "__main__":
    unittest.main()
