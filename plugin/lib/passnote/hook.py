"""Hook entrypoints (spec §5, §7, §9). main() never raises and never blocks the session."""
from __future__ import annotations

import json
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
    if not meta["rooms"]:
        # Maybe a /clear whose carry-over didn't finish (SessionStart fires once): finish it now.
        try:
            if not _carry_over_from_clear(sid, sessions.parse_pid(env.get("CLAUDE_PID"))):
                return None
        except paths.LockBusy:
            return None
        meta = sessions.load_meta(sid)
        if not meta["rooms"]:
            return None
    sessions.touch_active(sid, now)
    try:
        meta = _refresh_meta(sid, meta, inp, event, env)
        with sessions.session_lock(sid):
            return _deliver_locked(sid, meta, inp, event, env)
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

    def __init__(self, sid, meta, env, ahead=()):
        self.sid = sid
        self.me = meta.get("name")
        self.rooms = meta["rooms"]
        self.receiver_mode = _text(meta.get("permission_mode"))
        self.env = env
        self.settings_inbound = claude_settings.inbound(env.get("CLAUDE_PROJECT_DIR"))
        self.room_cap = max(1, OVERFLOW_CAP // max(1, len(self.rooms)))
        self.cache, self.taken = {}, {}
        self.items, self.held, self.seen = [], [], set()
        self.unread = []  # carried overflow refs beyond what one fire re-reads
        self.ahead = {}  # room -> refs taken ahead of the cursor, still ahead of it
        for ref in ahead:
            if (isinstance(ref, dict) and ref.get("room") in self.rooms and _int(ref.get("off"))
                    and _int(ref.get("seq")) and isinstance(ref.get("id"), str)):
                self.ahead.setdefault(ref["room"], []).append(ref)
        self.ahead_keys = {(ref["room"], ref["id"], ref["seq"]) for refs in self.ahead.values() for ref in refs}

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

    def verdict(self, room, msg):
        _, _, inbound, me = self.room(room)
        return trust.visibility(msg, self.sid, me, self.receiver_mode, inbound, self.env)

    def deliver(self, room, msg, ref, redeliver=False):
        members, display, _, me = self.room(room)
        self.items.append({"room": room, "display": display, "msg": msg, "members": members, "me": me,
                           "ref": ref, "redeliver": redeliver})
        self.seen.add((room, msg["id"], msg["seq"]))
        self.taken[room] = self.taken.get(room, 0) + 1

    def hold(self, room, msg, reason):
        """Never injected; shown to the human, and recorded so `who` doesn't count it as seen."""
        members, display, _, _ = self.room(room)
        self.held.append({"room": room, "display": display, "msg": msg, "reason": reason, "members": members})
        self.seen.add((room, msg["id"], msg["seq"]))
        store.append_event(room, {"type": "hold", "reason": reason, "id": msg["id"], "to_sid": self.sid})


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
    """Last fire's overflow, plus its emissions the transcript doesn't show. Each unconfirmed ref
    is re-rendered at most once: it comes back marked "redelivered", and a marked ref is never
    re-rendered again, so a transcript format drift can't redeliver forever (17a)."""
    emitted = [ref for ref in state["emitted"] if isinstance(ref, dict)]
    if emitted:
        found = transcript.delivered_ids(transcript_path, [ref.get("id") for ref in emitted])
        if found is None:
            if _once(fire.sid, ".transcript-unreadable"):
                paths.log_error("hook", note=TRANSCRIPT_UNREADABLE)
        else:
            for ref in emitted:
                if ref.get("id") in found or ref.get("redelivered"):
                    continue
                msg = _ref_message(ref, fire.rooms)
                if msg is not None:
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


def _deliver_locked(sid, meta, inp, event, env):
    state = sessions.load_emit(sid)
    fire = _Fire(sid, meta, env, state["ahead"])
    cfg = config.load()
    _take_from_last_fire(fire, state, _text(inp.get("transcript_path")) or _text(meta.get("transcript_path")))
    new_cursors = {}
    for room in fire.rooms:
        moved = _read_room(fire, room)
        if moved:
            new_cursors[room] = moved

    context, emitted, overflow = render.build(fire.items, fire.me, cfg["render_budget_chars"], cfg["clip_chars"])
    message = render.system_message(emitted, fire.held, fire.me)
    # Record what we emit, and what overflowed, before advancing cursors: if this process dies
    # before its output reaches Claude, the next fire finds these ids missing from the transcript
    # and redelivers them; overflow renders next fire.
    emit = {"emitted": [it["ref"] for it in emitted], "overflow": [it["ref"] for it in overflow] + fire.unread,
            "ahead": [ref for room in fire.rooms for ref in fire.ahead.get(room, ())]}
    if emit != state:
        sessions.save_emit(sid, emit["emitted"], emit["overflow"], emit["ahead"])
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
        # Unjoined: leave nothing for a later SessionStart(clear) to take over from.
        record = sessions.read_by_pid(pid)
        if record and "prev_sid" in record:
            del record["prev_sid"]
            paths.atomic_write_json(sessions.by_pid_path(pid), record)
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


CARRY_RETRY_WITHIN = 2.0  # seconds; the hook's own timeout is 5


def _carry_with_retry(sid, pid) -> None:
    """Carry over, and once more if a lock was busy and there is time left. A failure past that
    leaves the by-pid record as it is: the next delivery fire finishes the carry."""
    started = time.monotonic()
    try:
        _carry_over_from_clear(sid, pid)
    except paths.LockBusy:
        if time.monotonic() - started > CARRY_RETRY_WITHIN:
            raise
        _carry_over_from_clear(sid, pid)


def _carry_over_from_clear(sid, pid) -> bool:
    """Finish a /clear carry-over to `sid`, if the by-pid record SessionEnd(clear) left names an old
    session. It counts only for the same process (a reused pid has another start token) and only
    until record_pid rewrites the record without prev_sid. True if a carry-over ran. For a session
    with no rooms this costs one small file read."""
    if pid is None:
        return False
    record = sessions.read_by_pid(pid)
    prev = record.get("prev_sid") if record else None
    if not isinstance(prev, str):
        return False
    started = sessions.pid_started_at(pid)
    if not started or record.get("pid_started_at") != started or paths.check_sid(prev) == sid:
        return False
    rooms.carry_over(paths.check_sid(prev), sid)
    sessions.record_pid(pid, sid)
    return True


REMINDER_NAME_CHARS = 40
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
    return line


# `passnote post|claim|join|leave` as a command word: at the start, or after whitespace, a shell
# separator, a quote, "$(", a backtick, a backslash or a path slash. So "/abs/passnote post" and
# `bash -c "passnote post"` match; "mypassnote post" and "passnote-post" don't.
_WRITE_CMD = re.compile(
    r"""(?:^|[\s;&|(`$"'/\\])passnote["']?[ \t]+(?:--[ \t]+)?["']?(?:post|claim|join|leave)(?![\w-])""")


def handle_pre_tool_use(inp, event, env):
    """Subagents share the parent's session id (A8): don't let them post or change membership as the parent."""
    if not inp.get("agent_id") or inp.get("tool_name") != "Bash":
        return None
    tool_input = inp.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not _WRITE_CMD.search(command):
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": ("passnote: a subagent can't post or change membership as its parent "
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
