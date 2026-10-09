"""Room storage: append-only log, events, members and room meta (spec §4, §6)."""
from __future__ import annotations

import errno
import json
import os
import re
import time

from . import paths

MAX_READ = 256 * 1024
# The most bytes one read_from call reads (when it is more than its window). A multi-GB forged
# line, or a long run of oversized ones, must not push every hook fire past its 5 s timeout.
MAX_SKIP = 16 * 1024 * 1024
# Step for scanning to the end of an oversized line.
_SCAN_STEP = 4096
# How many of a member's earlier session ids (one per /clear) members.json remembers.
MAX_PREV_SIDS = 16
# A message id as a full-text file name: an alias (rooms._alias: up to 64 letters plus a short tail)
# and a seq. Anything else gets no path, so a forged id can never point outside the room's files/.
FULL_TEXT_ID_RE = re.compile(r"[a-z]{1,72}[0-9]{1,18}")


def log_path(room):
    return os.path.join(paths.room_dir(room), "log.jsonl")


def events_path(room):
    return os.path.join(paths.room_dir(room), "events.jsonl")


def members_path(room):
    return os.path.join(paths.room_dir(room), "members.json")


def meta_path(room):
    return os.path.join(paths.room_dir(room), "meta.json")


def full_text_dir(room):
    return os.path.join(paths.room_dir(room), "files")


def full_text_path(room, msg_id):
    """Where a long message's whole text lives (#27), derived from the room and a validated id,
    never read from the log: any member can forge a log line, so a path field could point anywhere."""
    if not isinstance(msg_id, str) or not FULL_TEXT_ID_RE.fullmatch(msg_id):
        return None
    return os.path.join(full_text_dir(room), f"{msg_id}.txt")


def room_lock(room, timeout=5.0):
    return paths.FileLock(os.path.join(paths.room_dir(room), ".lock"), timeout=timeout)


def parse(line):
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line.decode("utf-8"))
    except (ValueError, RecursionError):  # a deeply nested forged line must not block the room
        return None
    return obj if isinstance(obj, dict) else None


def valid_message(msg) -> bool:
    """Check if a message is well-formed (spec §4). Used to skip malformed log lines."""
    if not isinstance(msg, dict):
        return False
    # Required fields: seq, id, from, sid, kind, text must be str; seq must be int >= 1
    if not isinstance(msg.get("seq"), int) or isinstance(msg.get("seq"), bool) or msg.get("seq", 0) < 1:
        return False
    for field in ("id", "from", "sid", "kind", "text"):
        if not isinstance(msg.get(field), str):
            return False
    # to: required, "all" or a list of str
    to = msg.get("to")
    if to != "all" and not (isinstance(to, list) and all(isinstance(item, str) for item in to)):
        return False
    # re: absent/None or str
    if "re" in msg and msg.get("re") is not None and not isinstance(msg.get("re"), str):
        return False
    # mode: absent/None or str
    if "mode" in msg and msg.get("mode") is not None and not isinstance(msg.get("mode"), str):
        return False
    # wake: absent or bool
    if "wake" in msg and not isinstance(msg.get("wake"), bool):
        return False
    # ts: absent or int/float (not bool)
    if "ts" in msg and msg.get("ts") is not None:
        if isinstance(msg.get("ts"), bool) or not isinstance(msg.get("ts"), (int, float)):
            return False
    # thread: absent/None or str. A str that isn't a valid name counts as unthreaded
    # (render.thread_of), so a forged one is still delivered, never hidden (#25).
    if "thread" in msg and msg.get("thread") is not None and not isinstance(msg.get("thread"), str):
        return False
    # full_chars is deliberately not checked: a forged value must fail open, never hide the line.
    # render_line uses it only when it is an int (not bool) larger than the text, else ignores it.
    return True


def member_prefs(info):
    """(threads, digest) from a member entry: threads is a frozenset of valid thread names, or None
    for every thread (absent, or not a list of valid names: a forged value fails open); digest is
    True only for the JSON value true."""
    threads = info.get("threads") if isinstance(info, dict) else None
    if not (isinstance(threads, list) and all(paths.valid_member_name(t) for t in threads)):
        threads = None
    digest = isinstance(info, dict) and info.get("digest") is True
    return (frozenset(threads) if threads is not None else None), digest


def _open_append(path):
    paths.makedirs(os.path.dirname(path))
    return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)


def _write_record(fd, data) -> None:
    """One os.write, so an append stays atomic. A short write (ENOSPC, a signal) is a failed
    append: the fragment it leaves is repaired by the next append, but this one was not posted."""
    if os.write(fd, data) != len(data):
        raise OSError(errno.EIO, "short write")


def _ends_with_newline(path) -> bool:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                return True
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) == b"\n"
    except FileNotFoundError:
        return True


def last_seq(path) -> int:
    """Highest seq in the log. Seqs grow in file order, so the tail's max is the global max."""
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        return 0
    with open(path, "rb") as fh:
        for window in (8192, 65536, size):
            start = max(0, size - window)
            fh.seek(start)
            lines = fh.read(size - start).split(b"\n")
            if start > 0:
                lines = lines[1:]
            seqs = [msg["seq"] for msg in map(parse, lines) if msg and isinstance(msg.get("seq"), int)]
            if seqs:
                return max(seqs)
            if start == 0:
                return 0
    return 0


def _write_full_text(path, text) -> None:
    """Write text to path atomically: a fresh 0600 temp file in the same 0700 directory, then
    os.replace, so a reader never sees a partial file."""
    directory = paths.makedirs(os.path.dirname(path))
    tmp = os.path.join(directory, f".tmp-{os.getpid()}-{os.urandom(6).hex()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def append_message(room: str, rec: dict, alias: str, full_text=None) -> dict:
    """Append rec under the room lock. With full_text (#27), the whole text is written to its
    full-text file first and rec gains full_chars; if the log append then fails, the file is
    removed, so a delivered path always exists."""
    path = log_path(room)
    with room_lock(room):
        rec = dict(rec)
        rec["v"] = 1
        rec["seq"] = last_seq(path) + 1
        rec["id"] = f"{alias}{rec['seq']}"
        rec.setdefault("ts", round(time.time(), 3))
        written = None
        if full_text is not None:
            rec["full_chars"] = len(full_text)
            written = full_text_path(room, rec["id"])
            if written is None:
                raise paths.PassnoteError(f"invalid message id {rec['id']!r} for a full-text file", 2)
            _write_full_text(written, full_text)
        try:
            data = json.dumps(rec, ensure_ascii=True, sort_keys=True).encode("ascii") + b"\n"
            if not _ends_with_newline(path):
                data = b"\n" + data
            fd = _open_append(path)
            try:
                _write_record(fd, data)
            finally:
                os.close(fd)
        except BaseException:
            if written:
                try:
                    os.unlink(written)
                except FileNotFoundError:
                    pass
            raise
    return rec


def _past_next_newline(fh, limit, step):
    """Scan from fh's position for the next newline, reading at most `limit` bytes in `step`-byte
    reads: (offset just past it, bytes read, the bytes already read after it). The offset is None
    if the file ends, or the limit is reached, first. fh is left right after the bytes returned."""
    nread = 0
    while nread < limit:
        pos = fh.tell()
        block = fh.read(min(step, limit - nread))
        if not block:
            return None, nread, b""
        nread += len(block)
        found = block.find(b"\n")
        if found >= 0:
            return pos + found + 1, nread, block[found + 1:]
    return None, nread, b""


def _log_stall_once(path, fh, off) -> None:
    """One errors.log line per stalled log file and offset, not one per hook fire: a marker file
    keyed by inode and offset records it, so a replaced log is reported again."""
    ino = os.fstat(fh.fileno()).st_ino
    marker = os.path.join(os.path.dirname(path), f".oversized-{ino}-{int(off)}")
    try:
        os.close(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except OSError:
        return
    paths.log_error("read_from", OSError(errno.EFBIG, "oversized log lines exceed the read bound"))


def read_from(path, off, max_bytes=MAX_READ):
    """Complete lines from `off`: ([(line_offset, raw_bytes)], end_offset).

    One call reads at most max(MAX_SKIP, max_bytes) bytes in total, counting every byte read.

    A line longer than max_bytes can never come back whole. Rather than stall on it forever,
    skip it (and any oversized lines right after it) and return the complete lines that follow,
    as far as they fit in what is left of the read bound. Otherwise return ([], off):
    - nothing complete follows yet (the oversized line is the last one, or still being written):
      a later call, once something follows, returns it;
    - the oversized run from `off` is longer than the read bound: there is no safe point to
      resume from, so every call stays at `off` until the log is replaced. Logged once per log
      inode and offset.
    end_offset never lands just past an oversized line: every line it ends after is at most
    max_bytes long, which seq_before reads back whole, so a cursor saved there can be checked."""
    try:
        with open(path, "rb") as fh:
            return read_from_fh(fh, path, off, max_bytes)
    except FileNotFoundError:
        return [], off


def read_from_fh(fh, path, off, max_bytes=MAX_READ):
    """read_from on a file already open (so a caller that fstat'ed fh reads the file it measured)."""
    limit = max(MAX_SKIP, max_bytes)
    # Each byte is read once: `head` holds what the scan already read past a newline.
    step = min(_SCAN_STEP, max_bytes)
    fh.seek(off)
    start, used, head = off, 0, b""
    while True:
        more = fh.read(min(max_bytes - len(head), limit - used))
        used += len(more)
        chunk = head + more
        cut = chunk.rfind(b"\n")
        if cut >= 0:
            break  # chunk starts right after a newline, so its lines up to cut are whole
        if len(chunk) == max_bytes:  # an oversized line: skip to its end
            skip_to, scanned, head = _past_next_newline(fh, limit - used, step)
            used += scanned
            if skip_to is not None:
                start = skip_to
                continue
        if used >= limit:
            _log_stall_once(path, fh, off)
        return [], off
    lines, pos = [], start
    for raw in chunk[:cut + 1].split(b"\n")[:-1]:
        lines.append((pos, raw))
        pos += len(raw) + 1
    return lines, start + cut + 1


def read_at(path, off, length) -> bytes:
    try:
        with open(path, "rb") as fh:
            fh.seek(off)
            return fh.read(length)
    except FileNotFoundError:
        return b""


def seq_before(path, off):
    """Seq of the valid message (valid_message) whose line ends exactly at `off`, or None. Lets
    cursors detect a replaced log. Only a valid message is ever a cursor position, as the hook
    skips every other line, so a bare {"seq": N} line never verifies one.

    Reads back a small window first, then enough for any line read_from can return (up to
    MAX_READ bytes with its newline, plus the newline before it): a legitimate line of 4,000
    emoji is ~48 KB, and failing to read it back would reset the cursor on every fire."""
    if off <= 0:
        return None
    try:
        with open(path, "rb") as fh:
            for window in (32768, MAX_READ + 1):
                start = max(0, off - window)
                fh.seek(start)
                chunk = fh.read(off - start)
                if not chunk.endswith(b"\n"):
                    return None
                body = chunk[:-1]
                if start == 0 or b"\n" in body:
                    msg = parse(body.rpartition(b"\n")[2])
                    return msg["seq"] if valid_message(msg) else None
    except FileNotFoundError:
        return None
    return None


def iter_messages(room):
    """[(offset, msg)] for every parseable line. Streams the file (binary iteration splits on
    b"\\n" only) instead of holding the whole log and a split copy of it in memory (P7)."""
    out, pos = [], 0
    try:
        with open(log_path(room), "rb") as fh:
            for raw in fh:
                msg = parse(raw)
                if msg is not None:
                    out.append((pos, msg))
                pos += len(raw)
    except FileNotFoundError:
        return []
    return out


def append_event(room, ev: dict) -> None:
    ev = dict(ev)
    ev.setdefault("ts", round(time.time(), 3))
    data = json.dumps(ev, ensure_ascii=True, sort_keys=True).encode("ascii") + b"\n"
    fd = _open_append(events_path(room))
    try:
        _write_record(fd, data)
    finally:
        os.close(fd)


def tail_lines(path, max_bytes=MAX_READ):
    """The lines in the last max_bytes of a file, as raw bytes: [] if it is missing or unreadable.

    Splits on b"\\n" only. When the window did not start at offset 0 it also reads the byte before
    it: the first split element is then partial, or empty (that byte was a newline), and is
    dropped, so a window starting exactly at a line's first byte keeps that line. An unterminated
    last fragment is kept: parse() rejects a fragment that is not whole JSON, and a record
    written without its newline is still read."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            start = max(0, fh.tell() - max_bytes)
            fh.seek(max(0, start - 1))
            lines = fh.read(max_bytes + (start > 0)).split(b"\n")
    except OSError:
        return []
    if start > 0:
        lines = lines[1:]
    if lines and not lines[-1]:
        lines.pop()
    return lines


def read_events(room, max_bytes=MAX_READ):
    return [ev for ev in map(parse, tail_lines(events_path(room), max_bytes)) if ev]


def load_members(room) -> dict:
    """{sid: {"name", "alias", ...}}, dropping malformed entries (any member can write the file)."""
    members = paths.read_json(members_path(room), {})
    if not isinstance(members, dict):
        return {}
    out = {sid: info for sid, info in members.items()
           if isinstance(info, dict) and isinstance(info.get("name"), str) and isinstance(info.get("alias"), str)}
    for info in out.values():
        if "prev_sids" in info:
            prev = info.pop("prev_sids")
            if isinstance(prev, list):
                info["prev_sids"] = prev_sids(prev)
    return out


def prev_sids(values) -> list:
    """The valid session ids in `values`, the last MAX_PREV_SIDS of them."""
    return [sid for sid in values if isinstance(sid, str) and paths.SID_RE.fullmatch(sid)][-MAX_PREV_SIDS:]


def member_for_sid(members, sid):
    """(member sid, info) for the member whose session id is `sid` now, or was before a /clear
    (its prev_sids), else None. An earlier id that two members list matches neither: any member
    can write members.json, so such a claim proves nothing."""
    if not isinstance(members, dict) or not isinstance(sid, str):
        return None
    info = members.get(sid)
    if isinstance(info, dict):
        return sid, info
    found = [(member, info) for member, info in members.items()
             if isinstance(info, dict) and isinstance(info.get("prev_sids"), list) and sid in info["prev_sids"]]
    return found[0] if len(found) == 1 else None


def save_members(room, members) -> None:
    paths.atomic_write_json(members_path(room), members)


def load_meta(room) -> dict:
    meta = paths.read_json(meta_path(room), {})
    return meta if isinstance(meta, dict) else {}


def save_meta(room, meta) -> None:
    paths.atomic_write_json(meta_path(room), meta)
