import os
import unittest

from support import ROOT, HomeCase
from passnote import store, transcript

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "transcript_delivered.jsonl")


class TranscriptTest(HomeCase):
    def test_finds_only_passnote_delivered_ids(self):
        found = transcript.delivered_ids(FIXTURE, ["a1", "b2", "c3", "d4"])
        self.assertEqual(found, {"a1", "b2"})

    def test_a_ref_with_its_emitted_line_is_confirmed_only_by_that_whole_line(self):
        refs = [{"id": "a1", "line": "a1 alice→you ask: hello"},
                {"id": "a1", "line": "a1 alice→all say: same id, another room"},
                {"id": "b2", "line": "b2 bob→all say: hi"},  # emitted without the [api] prefix
                {"id": "b2"},  # emit state from before lines were recorded: matched by id
                {"id": "c3"}]
        self.assertEqual(transcript.unconfirmed(FIXTURE, refs), [refs[1], refs[2], refs[4]])
        self.assertIsNone(transcript.unconfirmed(None, refs))
        self.assertEqual(transcript.unconfirmed(FIXTURE, [{"id": ["x"]}, {"id": "a1", "line": 5}]),
                         [{"id": ["x"]}])

    def test_unreadable_returns_none(self):
        self.assertIsNone(transcript.delivered_ids(None, ["a1"]))
        self.assertIsNone(transcript.delivered_ids(os.path.join(self.tmp, "missing.jsonl"), ["a1"]))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads any file")
    def test_existing_but_unreadable_returns_none(self):
        path = os.path.join(self.tmp, "locked.jsonl")
        with open(path, "wb") as fh:
            fh.write(b"{}\n")
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o600)
        self.assertIsNone(transcript.delivered_ids(path, ["a1"]))

    def test_id_prefix_is_not_a_match(self):
        self.assertEqual(transcript.delivered_ids(FIXTURE, ["a"]), set())

    def test_malformed_transcript_never_raises(self):
        path = os.path.join(self.tmp, "bad.jsonl")
        with open(path, "wb") as fh:
            fh.write(b"\xff\xfe hook_additional_context\n")
            fh.write(b'{"attachment": ' + b"[" * 100000 + b" hook_additional_context\n")
            fh.write(b'{"attachment": 5, "x": "hook_additional_context"}\n')
            fh.write(b'{"attachment": {"type": "hook_additional_context", "content": {"a": 1}}}\n')
        self.assertEqual(transcript.delivered_ids(path, ["a1"]), set())


class TailLinesTest(HomeCase):
    def _write(self, data):
        path = os.path.join(self.tmp, "t.jsonl")
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_short_file_returns_every_line(self):
        self.assertEqual(store.tail_lines(self._write(b"one\ntwo\n")), [b"one", b"two"])

    def test_unterminated_last_line_is_kept(self):
        self.assertEqual(store.tail_lines(self._write(b"one\ntwo")), [b"one", b"two"])

    def test_partial_first_line_dropped_when_window_starts_mid_file(self):
        path = self._write(b"aaaa\nbbbb\ncccc\n")
        self.assertEqual(store.tail_lines(path, max_bytes=8), [b"cccc"])
        self.assertEqual(store.tail_lines(path, max_bytes=10), [b"bbbb", b"cccc"])  # starts right after the newline
        self.assertEqual(store.tail_lines(path, max_bytes=11), [b"bbbb", b"cccc"])  # starts on the newline

    def test_missing_file_is_empty(self):
        self.assertEqual(store.tail_lines(os.path.join(self.tmp, "missing")), [])

    def test_non_utf8_bytes_do_not_raise(self):
        self.assertEqual(store.tail_lines(self._write(b"\xff\xfe\nok\n")), [b"\xff\xfe", b"ok"])


if __name__ == "__main__":
    unittest.main()
