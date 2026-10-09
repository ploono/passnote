"""Hook entrypoints (spec §5, §7, §9). main() never raises and never blocks the session."""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time

from . import claude_settings, config, cursor, fold, paths, render, rooms, sessions, store, transcript, trust, wake

ERROR_NOTICE = "passnote: the hook hit an error; run `passnote doctor` (details in errors.log)"
TRANSCRIPT_UNREADABLE = "transcript unreadable; delivery confirmation skipped"
READ_NO_PROGRESS = "a full read window held no new valid line; delivery from this room is stuck at this offset"
# The most items one fire carries (rendered or overflowing), shared equally by the joined rooms.
# A fire renders about 2,000 characters, so this is a few fires' worth.
OVERFLOW_CAP = 64
# Addressed asks a room may take ahead of its full share per fire (see _Fire).
AHEAD_PER_ROOM = 4
# Seen receipts (#28): the sender's own addressed asks and props, reported once an addressee's turn
# has delivered them. Bounded: messages pending, names per message, age, and pairs shown per fire.
RECEIPT_KINDS = ("ask", "prop")
MAX_PENDING_RECEIPTS = 16
MAX_RECEIPT_ADDRESSEES = 4
RECEIPT_MAX_AGE = 86400  # seconds
RECEIPTS_PER_FIRE = 6
# Delivery evidence a session keeps: the addressed asks and props it delivered to its model.
MAX_DELIVERED = 64


def main(event, stdin_text, env=None) -> str:
    env = os.environ if env is None else env
    sid = None
    try:
        if sys.version_info < (3, 9):
            return ""
        paths.ensure_home()
        inp = json.loads(stdin_text or "{}")
        if not isinstance(inp, dict):
            return ""
        sid = inp.get("session_id")
        handler = HANDLERS.get(event)
        out = handler(inp, event, env) if handler else None
        return json.dumps(out, ensure_ascii=True) if out else ""
    except Exception as exc:
        return _fail(sid, event, exc)


def _once(sid, marker) -> bool:
    """True the first time only, per session and marker: O_EXCL makes the create-once atomic, so
    concurrent hooks (a PostToolBatch and a UserPromptSubmit) can't both show a notice."""
    path = os.path.join(paths.makedirs(paths.session_dir(sid)), marker)
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        return False
    return True


def _fail(sid, event, exc) -> str:
    """Log the failure (its type only), record it as the session's last error, and tell the
    human once per session. Only for joined sessions: an unjoined one gets no files."""
    try:
        paths.log_error(f"hook:{event}" if event in HANDLERS else "hook", exc)
        if not sid:
            return ""
        sid = paths.check_sid(sid)
        if not sessions.load_meta(sid)["rooms"]:
            return ""
        error = {"ts": round(time.time(), 3), "error": type(exc).__name__[:80]}

        def record(meta):
            if not meta["rooms"]:
                return False
            meta["last_error"] = error

        sessions.update_meta(sid, record)
        if _once(sid, ".error-shown"):
            return json.dumps({"systemMessage": ERROR_NOTICE})
    except Exception:
        pass
    return ""


def _int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _text(value):
    """A non-empty str, else None. A transcript_path that is an int would be a file descriptor."""
    return value if isinstance(value, str) and value else None


def handle_deliver(inp, event, env, now=None):
    if inp.get("agent_id"):
        return None  # a subagent's own hook: its session_id is the parent's (A8)
    sid = paths.check_sid(inp.get("session_id"))
    meta = sessions.load_meta(sid)
    healed = False
    if not meta["rooms"]:
        # Maybe a /clear whose carry-over didn't finish (SessionStart fires once): finish it now.
        try:
            healed = _carry_over_from_clear(sid, sessions.parse_pid(env.get("CLAUDE_PID")))
        except paths.LockBusy:
            return None
        meta = sessions.load_meta(sid)
        if not healed or not meta["rooms"]:
            return None
    sessions.touch_active(sid, now)
    try:
        meta = _refresh_meta(sid, meta, inp, event, env)
        with sessions.session_lock(sid):
            # The fire that healed a carry also says who the session is, as SessionStart would have
            # (#7). The heal released the lock: a fire that wins it first delivers without the line.
            return _deliver_locked(sid, meta, inp, event, env, reminder=_reminder(sid, meta) if healed else None)
    except paths.LockBusy:
        return None  # another fire holds the session or its meta: the next fire delivers


def _refresh_meta(sid, meta, inp, event, env):
    """Record what this fire's input says about the session. The slow parts (transcript, settings,
    rename) run before the meta lock; the write is one update_meta, skipped when nothing changed."""
    changes = {}
    if _text(inp.get("permission_mode")):
        changes["permission_mode"] = inp["permission_mode"]
    if _text(inp.get("transcript_path")):
        changes["transcript_path"] = inp["transcript_path"]
    prompt = event == "UserPromptSubmit"
    if prompt and isinstance(inp.get("session_title"), str):
        changes["title"] = inp["session_title"]
    if prompt or meta.get("ttl_seconds") is None:
        changes["ttl_seconds"] = wake.resolve_ttl(
            env,
            claude_settings.value("promptCacheTtl", env.get("CLAUDE_PROJECT_DIR")),
            wake.transcript_bucket(_text(changes.get("transcript_path", meta.get("transcript_path")))),
            config.load().get("ttl_seconds"),
        )
    if prompt and meta.get("name_source") in ("registry", "title"):
        name, source = sessions.resolve_name(None, sessions.parse_pid(env.get("CLAUDE_PID")),
                                             changes.get("title", meta.get("title")))
        if name and name != meta.get("name") and rooms.rename(sid, meta["rooms"], name):
            changes["name"], changes["name_source"] = name, source

    def apply(current):
        if all(current.get(key) == value for key, value in changes.items()):
            return False
        current.update(changes)

    # Most fires change nothing: don't take the meta lock for them.
    if all(meta.get(key) == value for key, value in changes.items()):
        return meta
    return sessions.update_meta(sid, apply)


class _Fire:
    """What one delivery fire collects across rooms.

    Back-pressure (review fix 1): the items a fire takes from the log are capped, OVERFLOW_CAP
    in all, an equal share per room. A room whose share is full is not read further this fire:
    its cursor stays, so the log itself holds the backlog and nothing is lost. The overflow
    carried in the emit state (re-read, re-checked and re-rendered every fire) stays within the
    cap plus what comes on top of it: last fire's unconfirmed emissions (once each) and up to
    AHEAD_PER_ROOM addressed asks per room taken ahead of a full share.

    Look-ahead (review fix 2): when a room's share is full, the rest of the window already read
    is scanned (no extra I/O) for addressed ask/err/--wake messages, which take up to
    AHEAD_PER_ROOM places beyond the share, so a deep ask isn't stuck behind the backlog
    (spec §7 order). Their refs stay in the emit state's "ahead" list until the cursor passes
    them, and the cursor pass skips them: each is delivered once."""

    def __init__(self, sid, meta, env, ahead=(), receipts=()):
        self.sid = sid
        self.me = meta.get("name")
        self.rooms = meta["rooms"]
        self.receiver_mode = _text(meta.get("permission_mode"))
        self.env = env
        self.settings_inbound = claude_settings.inbound(env.get("CLAUDE_PROJECT_DIR"))
        self.room_cap = max(1, OVERFLOW_CAP // max(1, len(self.rooms)))
        self.cache, self.prefs_cache, self.taken = {}, {}, {}
        self.items, self.held, self.seen = [], [], set()
        self.unread = []  # carried overflow refs beyond what one fire re-reads
        self.ahead = {}  # room -> refs taken ahead of the cursor, still ahead of it
        for ref in ahead:
            if (isinstance(ref, dict) and ref.get("room") in self.rooms and _int(ref.get("off"))
                    and _int(ref.get("seq")) and isinstance(ref.get("id"), str)):
                self.ahead.setdefault(ref["room"], []).append(ref)
        self.ahead_keys = {(ref["room"], ref["id"], ref["seq"]) for refs in self.ahead.values() for ref in refs}
        # This session's addressed asks and props waiting to be reported seen (see _receipts).
        now = time.time()
        self.pending = [entry for entry in receipts if _valid_receipt(entry, self.rooms, now)][-MAX_PENDING_RECEIPTS:]

    def track(self, room, msg):
        """The sender's own addressed ask or prop, passed by its own cursor: wait to report it seen."""
        to = msg.get("to")
        if msg.get("kind") not in RECEIPT_KINDS or not isinstance(to, list):
            return
        me = self.room(room)[3]
        names = [name for name in dict.fromkeys(to) if name != me and render.receipt_pair_ok(name, msg["id"])]
        names = names[:MAX_RECEIPT_ADDRESSEES]
        if names and not any(entry["room"] == room and entry["id"] == msg["id"] for entry in self.pending):
            now, ts = time.time(), msg.get("ts")
            # A log line's ts is only a hint (any member can write one): never later than now.
            ts = min(ts, now) if _finite(ts) else now
            self.pending.append({"room": room, "id": msg["id"], "seq": msg["seq"], "ts": ts,
                                 "mode": msg.get("mode"), "to": names})
            self.pending = self.pending[-MAX_PENDING_RECEIPTS:]

    def take_ahead(self, room, msg, ref):
        self.deliver(room, msg, ref)
        self.ahead.setdefault(room, []).append(dict(ref, seq=msg["seq"]))
        self.ahead_keys.add((room, msg["id"], msg["seq"]))

    def ahead_count(self) -> int:
        return sum(len(refs) for refs in self.ahead.values())

    def room(self, room):
        """(members, display, effective inbound, my name there) for a room, read once per fire.
        My name is the member name senders address in that room; meta's name is the fallback
        (a rename can land in the rooms while its meta update hit LockBusy)."""
        if room not in self.cache:
            display = store.load_meta(room).get("display")
            inbound = trust.effective_inbound(config.load(room)["inbound"], self.settings_inbound)
            members = store.load_members(room)
            me = members.get(self.sid, {}).get("name") or self.me
            self.cache[room] = (members, display if _text(display) else room, inbound, me)
        return self.cache[room]

    def full(self, room) -> bool:
        return self.taken.get(room, 0) >= self.room_cap

    def prefs(self, room):
        """(threads, digest) for this member in a room, from the members.json this fire already read."""
        if room not in self.prefs_cache:
            self.prefs_cache[room] = store.member_prefs(self.room(room)[0].get(self.sid))
        return self.prefs_cache[room]

    def verdict(self, room, msg):
        members, _, inbound, me = self.room(room)
        action, reason = trust.visibility(msg, self.sid, me, self.receiver_mode, inbound, self.env, members)
        if action == "deliver":
            threads, _ = self.prefs(room)
            thread = render.thread_of(msg)
            if (threads is not None and thread is not None and thread not in threads
                    and not self.always_arrives(room, msg, me)):
                # An unsubscribed thread (#25): done for this member. The cursor passes it, it is
                # never emitted, so it is never delivery evidence; `read --thread` still shows it.
                return "skip", None
        return action, reason

    def always_arrives(self, room, msg, me) -> bool:
        """Lines no subscription filters out (#25): those addressed to me by name; every prop, since
        silence counts as consent once the cursor passes it; and replies to my own posts (a broadcast
        reply inherits its parent's thread). My alias comes from the members.json this fire already
        read. A forged `re` only delivers more."""
        if msg.get("kind") == "prop" or render.addressed_by_name(msg, me):
            return True
        return render.replies_to(msg, self.alias(room))

    def alias(self, room):
        """My alias in a room, from the members.json this fire already read (None if not listed)."""
        info = self.room(room)[0].get(self.sid)
        return info.get("alias") if isinstance(info, dict) else None

    def deliver(self, room, msg, ref, redeliver=False):
        """Queue msg for rendering. In digest mode (#26) it is marked to fold into its thread's digest
        line unless render.whole says it arrives whole. This runs for redelivered and carried items
        too, so the mark always follows the current setting."""
        members, display, _, me = self.room(room)
        _, digest_on = self.prefs(room)
        self.items.append({"room": room, "display": display, "msg": msg, "members": members, "me": me,
                           "ref": ref, "redeliver": redeliver,
                           "digest": digest_on and not render.whole(msg, me, self.alias(room))})
        self.seen.add((room, msg["id"], msg["seq"]))
        self.taken[room] = self.taken.get(room, 0) + 1

    def hold(self, room, msg, reason):
        """Never injected; shown to the human, and recorded so `who` doesn't count it as seen."""
        members, display, _, _ = self.room(room)
        self.held.append({"room": room, "display": display, "msg": msg, "reason": reason, "members": members})
        self.seen.add((room, msg["id"], msg["seq"]))
        store.append_event(room, {"type": "hold", "reason": reason, "id": msg["id"], "to_sid": self.sid})


def _finite(value) -> bool:
    """A real, finite number: a NaN or infinite ts would never age out, and an int too large for a
    float (JSON allows one) would raise in the arithmetic."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _valid_receipt(entry, rooms_joined, now) -> bool:
    """A pending receipt as track() records it; anything else in the emit state is dropped, and so is a
    ts in the future (#31): it would never age out."""
    if not isinstance(entry, dict) or entry.get("room") not in rooms_joined:
        return False
    seq, to = entry.get("seq"), entry.get("to")
    return (isinstance(seq, int) and not isinstance(seq, bool) and seq >= 1 and _finite(entry.get("ts"))
            and entry["ts"] <= now
            and isinstance(to, list) and all(render.receipt_pair_ok(name, entry.get("id")) for name in to)
            and (entry.get("mode") is None or isinstance(entry.get("mode"), str)))


def _valid_evidence(entry) -> bool:
    """A delivery-evidence entry as _delivered() records it. Anything else proves nothing."""
    return (isinstance(entry, dict) and isinstance(entry.get("room"), str) and isinstance(entry.get("id"), str)
            and isinstance(entry.get("seq"), int) and not isinstance(entry.get("seq"), bool)
            and _finite(entry.get("ts")))


def _delivered(previous, emitted, now):
    """(evidence, added): this session's delivery evidence after a fire, and whether this fire added
    any. Evidence is the addressed (by name) asks and props it emitted to its model, as
    {room, id, seq, ts}. Only lines emitted whole count, never held, overflowing, digested or
    fallback ones (their text didn't reach the model), so a sender's receipt can't report a held
    message seen. Bounded: the last MAX_DELIVERED, none older than RECEIPT_MAX_AGE or dated in the
    future; malformed entries are dropped."""
    out = [entry for entry in previous[-MAX_DELIVERED:]
           if _valid_evidence(entry) and 0 <= now - entry["ts"] <= RECEIPT_MAX_AGE]
    keys = {(entry["room"], entry["id"], entry["seq"]) for entry in out}
    added = False
    for it in emitted:
        msg = it["msg"]
        key = (it["room"], msg["id"], msg["seq"])
        if (msg.get("kind") in RECEIPT_KINDS and render.addressed_by_name(msg, it.get("me"))
                and not it.get("digest") and not it.get("fallback") and key not in keys):
            keys.add(key)
            out.append({"room": it["room"], "id": msg["id"], "seq": msg["seq"], "ts": now})
            added = True
    return out[-MAX_DELIVERED:], added


def _receipt_labels(fire):
    """{room: label} for the receipt line: None for each room when the session is in one room (ids
    only repeat across rooms), else the room's display when it is a valid name no other joined room's
    label shares, and the room id otherwise. Room ids are unique valid names, so a shared label (two
    displays alike, or a display equal to another room's id) falls back to the ids: one label for two
    rooms would group their ids together."""
    if len(fire.rooms) <= 1:
        return {room: None for room in fire.rooms}
    labels = {}
    for room in fire.rooms:
        display = fire.room(room)[1]
        labels[room] = display if paths.valid_name(display) else room
    while True:
        counts = {}
        for label in labels.values():
            counts[label] = counts.get(label, 0) + 1
        shared = [room for room, label in labels.items() if counts[label] > 1 and label != room]
        if not shared:
            return labels
        for room in shared:
            labels[room] = room


def _receipts(fire, now):
    """(pairs, pending): the (name, id) pairs to report seen this fire, at most RECEIPTS_PER_FIRE
    and exactly those render.receipt_line shows, and the entries still pending.

    Seen needs positive evidence: the addressee's own emit state lists the message (room, id and
    seq) among those it delivered to its model. Held, overflowing, evicted, malformed or forged
    evidence proves nothing, so the pair stays pending until it ages out: a held message is never
    reported seen. The addressee's cursor and the room's events are not read. On top of that, the
    receiver's recorded mode against the sender's (fail-closed: their bypass env is invisible here)
    drops a name. An addressee who is gone or listed under an invalid sid is dropped for good. A
    seen pair the line has no room for stays for next fire."""
    pending, found, emits = [], [], {}
    labels = _receipt_labels(fire) if fire.pending else {}
    for entry in fire.pending:
        if now - entry["ts"] > RECEIPT_MAX_AGE:
            continue
        room, msg_id, seq = entry["room"], entry["id"], entry["seq"]
        members, _, inbound, _ = fire.room(room)
        label = labels.get(room)
        sid_of = {info["name"]: sid for sid, info in members.items()}
        kept = dict(entry, to=[])
        for name in dict.fromkeys(entry["to"]):
            if len(found) >= RECEIPTS_PER_FIRE:
                kept["to"].append(name)  # checked next fire
                continue
            target = sid_of.get(name)
            if target is None or not paths.valid_sid(target):
                continue  # gone, or a forged members.json key paths.check_sid would reject
            if target not in emits:
                emits[target] = sessions.load_emit(target)["delivered"][-MAX_DELIVERED:]
            if not any(_valid_evidence(ev) and (ev["room"], ev["id"], ev["seq"]) == (room, msg_id, seq)
                       for ev in emits[target]):
                kept["to"].append(name)  # not delivered (yet): held, overflowing or not read
                continue
            if trust.content_hold({"sid": fire.sid, "mode": entry.get("mode")}, sessions.recorded_mode(target),
                                  inbound, {}):
                continue
            kept["to"].append(name)  # until the line shows it
            found.append((kept, name, msg_id, label))
        pending.append(kept)
    _, shown = render.receipt_parts([(name, msg_id, label) for _, name, msg_id, label in found])
    left = len(shown)
    for kept, name, msg_id, label in found:
        valid = render.receipt_pair_ok(name, msg_id, label)
        if valid and left == 0:
            continue  # cut for length: stays pending
        kept["to"].remove(name)  # shown, or never showable
        left -= 1 if valid else 0
    return shown, [kept for kept in pending if kept["to"]]


def _ref_message(ref, rooms_joined):
    """The message an emit-state ref points at, or None if the ref is malformed, its room was
    left, or the log no longer holds that message there (replaced, truncated)."""
    if not isinstance(ref, dict) or ref.get("room") not in rooms_joined:
        return None
    off, length = ref.get("off"), ref.get("len")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (off, length)):
        return None
    if off < 0 or not 0 < length <= store.MAX_READ:
        return None
    msg = store.parse(store.read_at(store.log_path(ref["room"]), off, length))
    if not store.valid_message(msg) or msg["id"] != ref.get("id"):
        return None
    return msg


def _take_from_last_fire(fire, state, transcript_path):
    """Last fire's overflow, plus its emissions the transcript doesn't show (each emitted ref holds
    the exact line rendered for it). Each unconfirmed ref is re-rendered at most once: it comes
    back marked "redelivered", and a marked ref is never re-rendered again, so a transcript
    format drift can't redeliver forever (17a)."""
    emitted = [ref for ref in state["emitted"] if isinstance(ref, dict)]
    if emitted:
        missing = transcript.unconfirmed(transcript_path, emitted)
        if missing is None:
            if _once(fire.sid, ".transcript-unreadable"):
                paths.log_error("hook", note=TRANSCRIPT_UNREADABLE)
        else:
            for ref in missing:
                if ref.get("redelivered"):
                    continue
                msg = _ref_message(ref, fire.rooms)
                if msg is not None:
                    # Re-rendered below: the line it is emitted with then is recorded afresh.
                    ref = {key: value for key, value in ref.items() if key != "line"}
                    _admit(fire, ref["room"], msg, dict(ref, redelivered=True), redeliver=True)
    # At most OVERFLOW_CAP refs are re-read per fire; any beyond (an emit state from before the
    # cap, or a forged one) are carried unread, so a fire's work stays bounded.
    for ref in state["overflow"][:OVERFLOW_CAP]:
        msg = _ref_message(ref, fire.rooms)
        if msg is not None and (ref["room"], msg["id"], msg["seq"]) not in fire.seen:
            _admit(fire, ref["room"], msg, ref, redeliver=bool(ref.get("redelivered")))
    fire.unread = state["overflow"][OVERFLOW_CAP:]
    for ref in fire.unread:  # still ahead of anything newer in their room: keep their place
        if isinstance(ref, dict) and ref.get("room") in fire.rooms:
            fire.taken[ref["room"]] = fire.taken.get(ref["room"], 0) + 1


def _admit(fire, room, msg, ref, redeliver=False):
    """Deliver or hold a message carried from last fire, checked again: modes may have changed."""
    action, reason = fire.verdict(room, msg)
    if action == "deliver":
        fire.deliver(room, msg, ref, redeliver=redeliver)
    elif action == "hold":
        fire.hold(room, msg, reason)


def _log_no_progress(room, ino, off) -> None:
    """A whole read window with no valid line at or past the cursor (a run of junk or
    out-of-order lines) and more log beyond it: delivery from this room is stuck at `off`.
    Logged once per log inode and offset, like store's oversized-line stall."""
    marker = os.path.join(paths.room_dir(room), f".no-progress-{int(ino)}-{int(off)}")
    try:
        os.close(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except OSError:
        return
    paths.log_error("hook", note=READ_NO_PROGRESS)


def _read_room(fire, room):
    """Collect the room's new lines; return (cursor to save, reset, restarted), or None to keep
    the current one.

    The cursor ends just past the last valid line with a seq not below its own, so start() can
    always verify it next fire (a malformed final line would otherwise reset it every fire) and
    it never moves back: its seq goes down only for a restarted log. Held, filtered and
    already-seen lines still move it: they are done."""
    cur = cursor.load(fire.sid, room)
    if cur is None:
        return None
    log = store.log_path(room)
    try:
        st = os.stat(log)
    except FileNotFoundError:
        return None  # deleted: the next post recreates it, and start() resets to it
    off, dedupe, reset = cursor.start(cur, st, log)
    # Only a restarted log is deduped below our seq; only then may the saved seq go down.
    restarted = reset and dedupe < cur["seq"]
    if reset:
        store.append_event(room, {"type": "cursor-reset", "sid": fire.sid})
        # Ids are alias+seq, so a restarted log reuses them: refs taken ahead in the old log
        # must not make the cursor skip new messages (duplicates are allowed, losses are not).
        fire.ahead.pop(room, None)
        fire.ahead_keys = {key for key in fire.ahead_keys if key[0] != room}
    # A stalled read (a forged oversized run) returns ([], off): the cursor stays put (ruling 3).
    lines, end = store.read_from(log, off, store.MAX_READ)
    new_off, new_seq = cur["off"], cur["seq"]
    if reset:
        # start() resumes a replaced log just past a line it verified (seq at or below ours), or
        # at 0; the cursor starts there, so it stays verifiable even if nothing new follows.
        resumed = store.seq_before(log, off) if off else None
        new_off, new_seq = (off, resumed) if resumed is not None else (0, dedupe)
    capped = None
    for index, (line_off, raw) in enumerate(lines):
        if fire.full(room):
            capped = index  # back-pressure: the rest stays in the log for a later fire
            break
        msg = store.parse(raw)
        if not store.valid_message(msg) or msg["seq"] < new_seq:
            continue  # malformed, or out of order (forged): never a cursor position
        new_off, new_seq = line_off + len(raw) + 1, msg["seq"]
        key = (room, msg["id"], msg["seq"])
        if msg["seq"] <= dedupe or key in fire.seen or key in fire.ahead_keys:
            continue  # delivered before a reset, or taken already (last fire's emit state, or ahead)
        if trust.own(msg, fire.sid, fire.room(room)[0]):
            fire.track(room, msg)
        action, reason = fire.verdict(room, msg)
        if action == "deliver":
            fire.deliver(room, msg, {"room": room, "off": line_off, "len": len(raw), "id": msg["id"]})
        elif action == "hold":
            fire.hold(room, msg, reason)
    if capped is not None:
        _look_ahead(fire, room, lines[capped:], max(dedupe, new_seq))
    elif lines and new_off == off and end < st.st_size:
        _log_no_progress(room, st.st_ino, off)
    # Refs taken ahead stay recorded until the cursor passes them.
    fire.ahead[room] = [ref for ref in fire.ahead.get(room, ()) if ref["off"] >= new_off]
    new = {"ino": st.st_ino, "off": new_off, "seq": new_seq}
    if not restarted and new_seq < cur["seq"]:
        # A reset whose resume point is below our seq (our line is gone, or nothing at or above
        # it was read yet): keep the old cursor, never move back (start() resumes again next fire).
        return None
    if reset or new != cur:
        return new, reset, restarted
    return None


def _look_ahead(fire, room, rest, floor):
    """Take up to AHEAD_PER_ROOM addressed ask/err/--wake messages (render priority 0) from the
    part of the read window a full share left unread (spec §7: they come first)."""
    _, _, _, me = fire.room(room)
    taken = 0
    for line_off, raw in rest:
        if taken >= AHEAD_PER_ROOM or fire.ahead_count() >= OVERFLOW_CAP:
            return
        if not (b'"ask"' in raw or b'"err"' in raw or b'"wake"' in raw):
            continue  # cheap pre-check: only these kinds and --wake rank first
        msg = store.parse(raw)
        if not store.valid_message(msg) or msg["seq"] <= floor or render.priority(msg, me) != 0:
            continue
        key = (room, msg["id"], msg["seq"])
        if key in fire.seen or key in fire.ahead_keys or fire.verdict(room, msg)[0] != "deliver":
            continue  # a held one waits for the cursor pass, which holds and records it
        fire.take_ahead(room, msg, {"room": room, "off": line_off, "len": len(raw), "id": msg["id"]})
        taken += 1


def _deliver_locked(sid, meta, inp, event, env, reminder=None):
    state = sessions.load_emit(sid)
    fire = _Fire(sid, meta, env, state["ahead"], state["receipts"])
    cfg = config.load()
    _take_from_last_fire(fire, state, _text(inp.get("transcript_path")) or _text(meta.get("transcript_path")))
    new_cursors = {}
    for room in fire.rooms:
        moved = _read_room(fire, room)
        if moved:
            new_cursors[room] = moved

    addressed_clip = max(cfg["clip_chars"], cfg["clip_addressed_chars"])
    for it in fire.items:
        it["clip"] = addressed_clip if render.addressed_by_name(it["msg"], it["me"]) else cfg["clip_chars"]
    # Receipts cost nothing when none is pending. A shown receipt is done: it is never recorded for
    # transcript confirmation, so each is shown at most once.
    pairs, fire.pending = _receipts(fire, time.time()) if fire.pending else ([], [])
    # The reminder (a healing fire only) and the receipt line end the context, inside build's caps.
    tail = "\n".join(part for part in (reminder, render.receipt_line(pairs)) if part) or None
    context, emitted, overflow = render.build(fire.items, fire.me, cfg["render_budget_chars"], cfg["clip_chars"],
                                              tail=tail)
    message = render.system_message(emitted, fire.held, fire.me)
    # Record what we emit, and what overflowed, before advancing cursors: if this process dies
    # before its output reaches Claude, the next fire finds these lines missing from the transcript
    # and redelivers them; overflow renders next fire. An emitted ref keeps its exact line (bounded
    # by the render caps): ids repeat across rooms, so an id alone can't confirm a delivery.
    delivered, added = _delivered(state["delivered"], emitted, time.time())
    emit = {"emitted": [dict(it["ref"], line=it["line"]) for it in emitted],
            "overflow": [it["ref"] for it in overflow] + fire.unread,
            "ahead": [ref for room in fire.rooms for ref in fire.ahead.get(room, ())],
            "receipts": fire.pending,
            "delivered": state["delivered"]}
    if emit != state or added:
        # Evidence is pruned (aged out, future-dated, malformed) only in a save that happens anyway
        # (#31): pruning alone never writes on an otherwise idle fire.
        emit["delivered"] = delivered
        sessions.save_emit(sid, emit["emitted"], emit["overflow"], emit["ahead"], emit["receipts"],
                           emit["delivered"])
    for room, (new, was_reset, was_restarted) in new_cursors.items():
        cursor.save(sid, room, new, reset=was_reset, restarted=was_restarted)
    out = {}
    if context:
        out["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": context}
    if message:
        out["systemMessage"] = message
    return out or None


def handle_session_end(inp, event, env):
    """SessionEnd(clear): remember the old sid under our pid, because SessionStart(clear) can't see it (A2)."""
    if inp.get("reason") != "clear" or inp.get("agent_id"):
        return None
    sid = paths.check_sid(inp.get("session_id"))
    pid = sessions.parse_pid(env.get("CLAUDE_PID"))
    if pid is None:
        return None
    if not sessions.load_meta(sid)["rooms"]:
        # Unjoined. If a /clear's carry to this session never finished, it is cleared again before any
        # fire (#7): finish the carry now, so the membership moves on with this /clear instead of
        # being stranded with the old session.
        try:
            _carry_over_from_clear(sid, pid)
        except paths.LockBusy:
            return None  # the record keeps prev_sid: the next SessionStart(clear) carries from it
        if not sessions.load_meta(sid)["rooms"]:
            record = sessions.read_by_pid(pid)
            if record and record.get("prev_sid") == sid:
                _drop_prev_sid(pid, record)  # never a carry source while it has no rooms
            return None
    try:
        sessions.record_pid(pid, sid, prev_sid=sid)
    except paths.LockBusy:
        pass  # the pid record is written first; only its mirror into the meta was skipped
    return None


def handle_session_start(inp, event, env):
    if inp.get("agent_id"):
        return None
    sid = paths.check_sid(inp.get("session_id"))
    source = inp.get("source")
    pid = sessions.parse_pid(env.get("CLAUDE_PID"))
    try:
        if source == "clear":
            _carry_with_retry(sid, pid)
        meta = sessions.load_meta(sid)
        if not meta["rooms"]:
            return None
        changes = {}
        if _text(inp.get("transcript_path")):
            changes["transcript_path"] = inp["transcript_path"]
        if isinstance(inp.get("session_title"), str):
            changes["title"] = inp["session_title"]

        def apply(current):
            if all(current.get(key) == value for key, value in changes.items()):
                return False
            current.update(changes)

        # The meta first, the pid record after: write_by_pid mirrors pid into the meta, and a stale
        # meta dict written back later would undo that.
        meta = sessions.update_meta(sid, apply)
        if pid is not None:
            sessions.record_pid(pid, sid)
    except paths.LockBusy:
        return None  # next fire
    if source in ("clear", "resume", "compact"):
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": _reminder(sid, meta)}}
    return None


# Of SessionStart's 5 s hook timeout, the most the /clear carry may use (#7). A retry after a busy lock
# runs only if a whole one still fits: a room-lock wait (the time left, at least CARRY_MIN_RETRY) plus
# a meta-lock wait (sessions.META_LOCK_TIMEOUT). rooms.carry_over bounds its room-lock waits in all,
# not per room, so this holds however many rooms move. It is best effort: an attempt's record_pid
# can wait on the meta lock once more, and each attempt runs `ps`. An overrun that gets the hook
# killed is safe: a carry is idempotent, and until record_pid drops prev_sid the next delivery fire
# finishes it.
CARRY_BUDGET = 4.5
CARRY_MIN_RETRY = 0.5
_clock = time.monotonic  # module-level, so tests can drive the retry budget


def _carry_with_retry(sid, pid) -> bool:
    """Carry over, and once more if a lock was busy and a whole retry still fits CARRY_BUDGET. A
    failure past that leaves the by-pid record as it is: the next delivery fire finishes the carry."""
    started = _clock()
    try:
        return _carry_over_from_clear(sid, pid)
    except paths.LockBusy:
        left = CARRY_BUDGET - (_clock() - started) - sessions.META_LOCK_TIMEOUT
        if left < CARRY_MIN_RETRY:
            raise
        return _carry_over_from_clear(sid, pid, lock_timeout=min(left, rooms.CARRY_LOCK_TIMEOUT))


def _drop_prev_sid(pid, record) -> None:
    """Rewrite the by-pid record without prev_sid: guard.sh then stops sending this process's fires
    to Python for a carry that can never run."""
    paths.atomic_write_json(sessions.by_pid_path(pid), {k: v for k, v in record.items() if k != "prev_sid"})


def _carry_over_from_clear(sid, pid, lock_timeout=rooms.CARRY_LOCK_TIMEOUT) -> bool:
    """Finish a /clear carry-over to `sid`, if the by-pid record SessionEnd(clear) left names an old
    session. It counts only for the same process (a reused pid has another start token) and only
    until record_pid rewrites the record without prev_sid. True if a carry-over ran.

    - A prev_sid that isn't a session id, or a record whose start token is known and differs (another
      process got this pid), is dropped, so no later fire pays for it again (#7). A token that can't be
      read (`ps` failed) keeps the record: try again next fire.
    - The carry runs under `sid`'s delivery lock, non-blocking (#7): no fire of this session can save
      its emit state while the old one moves in. Lock order: session .lock, room locks, meta lock (as
      in `passnote leave`). LockBusy propagates.

    For a session with no rooms and no pending carry this costs one small file read."""
    if pid is None:
        return False
    record = sessions.read_by_pid(pid)
    prev = record.get("prev_sid") if record else None
    if not isinstance(prev, str):
        return False
    if not paths.valid_sid(prev):
        _drop_prev_sid(pid, record)
        return False
    started = sessions.pid_started_at(pid)
    if started is None:
        return False
    if record.get("pid_started_at") != started:
        _drop_prev_sid(pid, record)
        return False
    prev = paths.check_sid(prev)
    if prev == sid:
        return False
    with sessions.session_lock(sid):
        rooms.carry_over(prev, sid, lock_timeout=lock_timeout)
        sessions.record_pid(pid, sid)
    return True


REMINDER_NAME_CHARS = 40
# The SessionStart / healing-fire reminder, as JSON: forged room displays or names (each up to 40
# characters, any of which can cost 12 bytes escaped) must not push the hook output past 8 KB.
REMINDER_MAX_JSON = 1000
# Rooms the reminder names before "+N": it stays one short line however many rooms there are.
REMINDER_ROOMS_SHOWN = 5


def _reminder(sid, meta) -> str:
    """The one line SessionStart injects for a joined session: who it is, where, and what waits."""
    displays, waiting, first = [], [], None
    for room in meta["rooms"]:
        displays.append(render.gist(store.load_meta(room).get("display") or room, REMINDER_NAME_CHARS))
        members = store.load_members(room)
        me = members.get(sid, {}).get("name") or meta.get("name") or "?"
        first = first or me
        state = fold.fold([msg for _, msg in store.iter_messages(room)], members, {})
        waiting += [item["id"] for item in state["unanswered"] if me in item["waiting_on"]]
    me = render.gist(meta.get("name") or first or "?", REMINDER_NAME_CHARS)
    shown_rooms = ", ".join(displays[:REMINDER_ROOMS_SHOWN])
    if len(displays) > REMINDER_ROOMS_SHOWN:
        shown_rooms += f" +{len(displays) - REMINDER_ROOMS_SHOWN}"
    line = f"passnote: you are {me} in rooms {shown_rooms}; /passnote for the protocol"
    if waiting:
        shown = ", ".join(render.gist(ref, REMINDER_NAME_CHARS) for ref in waiting[:10])
        line += f". Waiting on your reply: {shown} (passnote read --id ID)"
    if len(json.dumps(line, ensure_ascii=True)) - 2 > REMINDER_MAX_JSON:
        line = (f"passnote: you are a member of {len(meta['rooms'])} room(s); passnote rooms lists them; "
                "/passnote for the protocol")
    return line


# A state-changing verb (`passnote post|claim|join|leave|subscribe|unsubscribe|digest`) as a command
# word: at the start, or after whitespace, a shell separator, a quote, "$(", a backtick, a backslash or
# a path slash. So "/abs/passnote post" and `bash -c "passnote post"` match; "mypassnote post" and
# "passnote-post" don't.
_WRITE_CMD = re.compile(
    r"""(?:^|[\s;&|(`$"'/\\])passnote["']?[ \t]+(?:--[ \t]+)?["']?"""
    r"""(?:post|claim|join|leave|subscribe|unsubscribe|digest)(?![\w-])""")


def handle_pre_tool_use(inp, event, env):
    """Subagents share the parent's session id (A8): don't let them post or change membership or
    subscriptions as the parent."""
    if not inp.get("agent_id") or inp.get("tool_name") != "Bash":
        return None
    tool_input = inp.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not _WRITE_CMD.search(command):
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": ("passnote: a subagent can't post or change membership or subscriptions as its parent "
                                     "session; report back to the parent instead (if the command only mentions passnote, "
                                     "in grep, echo or a message, reword it so 'passnote <verb>' is not a command word)"),
    }}


HANDLERS = {
    "UserPromptSubmit": handle_deliver,
    "PostToolBatch": handle_deliver,
    "SessionStart": handle_session_start,
    "SessionEnd": handle_session_end,
    "PreToolUse": handle_pre_tool_use,
}
