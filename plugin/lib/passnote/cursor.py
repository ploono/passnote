"""Per-session cursors {ino, off, seq} (spec §7 steps 4 and 8, F16)."""
from __future__ import annotations

import os

from . import paths, store


def path(sid, room):
    paths.check_name(room, member=False)
    return os.path.join(paths.session_dir(sid), "cursors", f"{room}.json")


def load(sid, room):
    cur = paths.read_json(path(sid, room))
    if isinstance(cur, dict) and all(isinstance(cur.get(key), int) for key in ("ino", "off", "seq")):
        return {"ino": cur["ino"], "off": cur["off"], "seq": cur["seq"]}
    return None


def at_eof(room) -> dict:
    """A cursor at the end of the log, or rather just past its last line that start() can verify:
    a cursor after a malformed or unterminated last line would reset, and redeliver the whole
    log, on every fire."""
    log = store.log_path(room)
    if not os.path.exists(log):
        paths.makedirs(os.path.dirname(log))
        os.close(os.open(log, os.O_WRONLY | os.O_CREAT, 0o600))
    st = os.stat(log)
    # With no verifiable line in reach: (0, the log's last seq), which start() accepts as is
    # and which still skips every message already in the log.
    off, seq = _last_position(log, st.st_size) or (0, store.last_seq(log))
    return {"ino": st.st_ino, "off": off, "seq": seq}


def _last_position(log, size, at_most=None):
    """(offset just past the last line the hook itself could have saved a cursor after, with seq
    at most `at_most` if given, and that line's seq), or None.

    The hook's rule (hook._read_room): a line is a cursor position only if it is a valid message
    (store.valid_message) whose seq is not below the running max seq of the valid lines before it.
    A forward pass applies that rule, so forged lines after the newest message, whole messages or
    bare {"seq": N}, in any order, are never chosen: resuming after one would skip everything
    before it. Lines of MAX_READ bytes or more are passed over, as read_from passes over them.

    One pass over the last MAX_SKIP bytes (the most one read_from call reads; a fraction of a
    second when full), not the last read window first: forged lines can fill a read window, in
    order among themselves. A window that does not start at the log's first byte can't tell
    whether its first valid line is in order, so that line alone is never a position."""
    start = max(0, size - max(store.MAX_SKIP, store.MAX_READ + 1))
    with open(log, "rb") as fh:
        fh.seek(start)
        data = fh.read(size - start)
    # The window's first line starts before it (with no newline at all, nothing is read).
    pos = data.find(b"\n") + 1 if start > 0 else 0
    top = None  # the highest seq of the valid lines so far
    found = None
    while True:
        end = data.find(b"\n", pos)
        if end < 0:
            break
        line = data[pos:end]
        pos = end + 1
        # Skipping a line with no literal "seq" key before parsing it bounds the cost of a window of
        # forged junk. A line hiding its seq behind \u escapes is then never a position: only a
        # forged line looks like that, and resuming before it at worst redelivers (duplicates allowed).
        if len(line) >= store.MAX_READ or b'"seq"' not in line:
            continue
        msg = store.parse(line)
        if not store.valid_message(msg) or (top is not None and msg["seq"] < top):
            continue
        if (at_most is None or msg["seq"] <= at_most) and (top is not None or start == 0):
            found = (start + end + 1, msg["seq"])
        top = msg["seq"]
    # seq_before reads every such line back (a line under MAX_READ bytes): a check, not a filter.
    if found and store.seq_before(log, found[0]) == found[1]:
        return found
    return None


def start(cur, st, log_path):
    """Where to resume reading, and the seq at or below which lines were already delivered.

    Not the same file (replaced, truncated, rewritten):
    - a new inode whose line ending at our offset has our seq (a copy, an editor's save): resume
      there, (off, seq, True);
    - its newest seq is not beyond ours: it was restarted, so redeliver everything (duplicates
      are allowed): (0, 0, True). Only then is the dedupe seq below ours, and only then may the
      cursor's seq go down (save(..., restarted=True));
    - else it still holds what we read (e.g. a copy): resume just past its last line at or below
      our seq that the hook could have saved a cursor after (_last_position), as seqs grow in
      file order (review fix 2). Re-reading from 0 instead would stall for good once more than a
      read window of old lines precedes the new ones. With no such line in reach: (0, our seq,
      True). The resume point's own seq may be below ours (our line is gone); the cursor is
      saved only once the read passes a line at or above our seq (save keeps it from going down)."""
    same_file = (cur["ino"] == st.st_ino and cur["off"] <= st.st_size
                 and (cur["off"] == 0 or store.seq_before(log_path, cur["off"]) == cur["seq"]))
    if same_file:
        return cur["off"], cur["seq"], False
    if 0 < cur["off"] <= st.st_size and store.seq_before(log_path, cur["off"]) == cur["seq"]:
        return cur["off"], cur["seq"], True  # a new inode with our line in place: a copy
    if store.last_seq(log_path) <= cur["seq"]:
        return 0, 0, True
    found = _last_position(log_path, st.st_size, at_most=cur["seq"])
    return (found[0] if found else 0), cur["seq"], True


def save(sid, room, new, reset=False, restarted=False) -> bool:
    """Write a cursor unless it would move back; return whether it was written.

    Its seq never goes down unless the log was restarted (start() gave dedupe 0 below our seq):
    a replaced log is still deduped by our seq (spec §7), so a lower seq would point before
    lines already delivered, or past ones not yet delivered after a forged resume point. Its
    offset never goes down on the same inode unless the log was reset (rewritten in place)."""
    old = load(sid, room)
    if old and not restarted and new["seq"] < old["seq"]:
        return False
    if old and not reset and old["ino"] == new["ino"] and new["off"] < old["off"]:
        return False
    paths.atomic_write_json(path(sid, room), {"ino": int(new["ino"]), "off": int(new["off"]), "seq": int(new["seq"])})
    return True


def remove(sid, room) -> None:
    try:
        os.unlink(path(sid, room))
    except FileNotFoundError:
        pass
