"""passnote command-line interface (spec §10).

Exit codes: 0 ok, 2 usage or room error, 3 not joined, 4 text too long, 130 interrupted.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time

from . import __version__, claude_settings, config, cursor, fold, paths, render, rooms, sessions, store, trust, wake

KINDS = ("say", "ask", "ans", "nak", "prop", "done", "err", "claim", "status")
GIST_CHARS = 80  # a message's gist in who and watch
POLL_SECONDS = 1.0  # how often watch looks for new lines
# How long `leave` waits for a hook fire that holds the session's delivery lock.
LEAVE_LOCK_TIMEOUT = 2.0


def build_parser():
    parser = argparse.ArgumentParser(prog="passnote", description="Token-lean messages between Claude Code sessions.")
    parser.add_argument("--version", action="version", version=f"passnote {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # --room is an option of each subcommand, never of the parser: `passnote --room r post` would
    # not match the subagent write guard's `passnote <verb>` pattern (hook._WRITE_CMD).
    p = sub.add_parser("join", help="join a room (default: this project's room)")
    p.add_argument("room", nargs="?")
    p.add_argument("--as", dest="as_name", metavar="NAME")
    p.set_defaults(func=cmd_join, needs_home=True)

    p = sub.add_parser("leave", help="leave a room")
    p.add_argument("--room")
    p.set_defaults(func=cmd_leave, needs_home=True)

    p = sub.add_parser("rooms", help="list the rooms this session joined")
    p.set_defaults(func=cmd_rooms, needs_home=True)

    p = sub.add_parser("post", help="post a message; the text is read from stdin")
    p.add_argument("--room")
    p.add_argument("--to", help="comma-separated member names (default: all)")
    p.add_argument("--kind", default="say", choices=KINDS)
    p.add_argument("--re", dest="re_id", metavar="ID")
    urgency = p.add_mutually_exclusive_group()
    urgency.add_argument("--wake", action="store_true", help="doorbell if the addressee is warm")
    urgency.add_argument("--urgent", action="store_true", help="doorbell even if the addressee is cold")
    p.add_argument("--allow-secret-looking", action="store_true")
    p.set_defaults(func=cmd_post, needs_home=True)

    p = sub.add_parser("claim", help="claim a piece of work, or release a claim")
    p.add_argument("what", nargs="?")
    p.add_argument("--release", metavar="ID")
    p.add_argument("--room")
    p.set_defaults(func=cmd_claim, needs_home=True)

    p = sub.add_parser("read", help="read messages without moving your cursor")
    p.add_argument("--room")
    which = p.add_mutually_exclusive_group()
    which.add_argument("--id")
    which.add_argument("--since", metavar="ID")
    which.add_argument("--last", type=int, default=20)
    p.set_defaults(func=cmd_read, needs_home=True)

    # `who` and `watch` only read, and uninstall doesn't need the storage root: they don't create it
    # (`who` in a fresh home leaves it absent) but still refuse one others can write to ("check").
    # shim doesn't touch it at all.
    p = sub.add_parser("who", help="members, warmth, unanswered asks, claims, status, props")
    p.add_argument("--room")
    p.set_defaults(func=cmd_who, needs_home="check")

    p = sub.add_parser("watch", help="live view of room messages and events (human terminal)")
    p.add_argument("room", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--last", type=int, default=20)
    p.add_argument("--once", action="store_true", help="print recent activity and exit")
    p.set_defaults(func=cmd_watch, needs_home="check")

    p = sub.add_parser("gc", help="prune stale sessions and pid records")
    p.add_argument("--days", type=int, default=7)
    p.set_defaults(func=cmd_gc, needs_home=True)

    p = sub.add_parser("uninstall", help="delete all passnote data")
    p.add_argument("--purge", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_uninstall, needs_home="check")

    p = sub.add_parser("shim", help="install a stable `passnote` command for your terminal")
    p.add_argument("--path", default="~/.local/bin/passnote")
    p.add_argument("--force", action="store_true", help="overwrite a file that is not a passnote shim")
    p.set_defaults(func=cmd_shim, needs_home=False)

    # doctor must run, and report, when the storage root is missing or unusable: it checks it itself.
    p = sub.add_parser("doctor", help="check the installation and this session")
    p.set_defaults(func=cmd_doctor, needs_home=False)

    return parser


def main(argv=None, stdin=None, stdout=None, stderr=None, env=None) -> int:
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    env = os.environ if env is None else env
    if (sys.argv[1:] if argv is None else list(argv))[:1] == ["--"]:
        # Python >= 3.12 argparse would accept `passnote -- post`, which the subagent write guard
        # (hook._WRITE_CMD) is not meant to have to reason about beyond its documented forms.
        stderr.write("passnote: a subcommand is required before any --\n")
        return 2
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        needs_home = getattr(args, "needs_home", True)
        if needs_home == "check":
            paths.check_home()
        elif needs_home:
            paths.ensure_home()
        code = args.func(args, stdin, stdout, env) or 0
        stdout.flush()  # a broken pipe surfaces here, not at interpreter exit
        return code
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # The reader went away (`passnote read | head`). Python's recipe: point stdout at devnull so the
        # flush at exit can't fail again, and say nothing.
        _silence(stdout)
        return 1
    except paths.PassnoteError as exc:
        stderr.write(f"passnote: {exc}\n")
        return exc.code
    except paths.LockBusy:
        stderr.write("passnote: the room is busy; try again\n")
        return 2
    except OSError as exc:
        if _under_home(exc.filename):
            stderr.write(f"passnote: cannot write {paths.home()}: {exc.strerror or exc}; with the sandbox on, "
                         "add it to sandbox.filesystem.allowWrite (see passnote doctor)\n")
        else:
            stderr.write(f"passnote: {exc.strerror or type(exc).__name__}\n")
        return 2
    except Exception as exc:  # last resort: one line, never a traceback
        paths.log_error("cli", exc)
        stderr.write(f"passnote: internal error ({type(exc).__name__}); see errors.log\n")
        return 2


def _under_home(filename) -> bool:
    """Whether a failed path is the storage root, inside it, or a parent being created for it."""
    if not isinstance(filename, str):
        return False
    home = paths.home()
    return filename == home or filename.startswith(home + os.sep) or home.startswith(filename.rstrip(os.sep) + os.sep)


def _silence(stdout) -> None:
    if stdout is not sys.stdout:
        return
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except (OSError, ValueError):
        pass


def _session(env):
    sid = sessions.current_sid(env)
    return sid, sessions.load_meta(sid)


def _viewer(env):
    """(sid, meta) inside a Claude Code session; (None, None) in a human terminal."""
    if not env.get("CLAUDE_CODE_SESSION_ID"):
        return None, None
    return _session(env)


def _all_rooms():
    root = os.path.join(paths.home(), "rooms")
    if not os.path.isdir(root):
        return []
    return sorted(name for name in os.listdir(root) if paths.valid_name(name))


def _room(args, meta):
    explicit = getattr(args, "room", None)
    if explicit:
        return paths.check_name(explicit, member=False)
    if meta is None:
        existing = _all_rooms()
        if len(existing) == 1:
            return existing[0]
        raise paths.PassnoteError("pass --room (rooms: " + (", ".join(existing) or "none") + ")", 2)
    if len(meta["rooms"]) == 1:
        return meta["rooms"][0]
    if not meta["rooms"]:
        raise paths.PassnoteError("not joined to any room; run: passnote join", 3)
    raise paths.PassnoteError("joined to several rooms; pass --room (one of: " + ", ".join(meta["rooms"]) + ")", 2)


def _existing_room(room):
    if not os.path.isdir(paths.room_dir(room)):
        raise paths.PassnoteError(f"no such room: {render.escape_text(room)}", 2)
    return room


def _members_or_exit(sid, room):
    members = store.load_members(room)
    if sid not in members:
        raise paths.PassnoteError(f"not joined to {room}; run: passnote join", 3)
    return members


def _valid_messages(room):
    """[msg] for the log's well-formed lines only (spec §4): the one place the CLI looks into the log."""
    return [msg for _, msg in store.iter_messages(room) if store.valid_message(msg)]


def cmd_join(args, stdin, stdout, env):
    sid, meta = _session(env)
    if args.room:
        room = paths.check_name(args.room, member=False)
        display, root = room, os.path.realpath(os.getcwd())
    else:
        room, display, root = rooms.default_room(os.getcwd())
    pid = sessions.parse_pid(env.get("CLAUDE_PID"))
    if not args.as_name and meta["rooms"] and meta.get("name"):
        name, source = meta["name"], meta.get("name_source")  # one name per session
    else:
        name, source = sessions.resolve_name(args.as_name, pid, meta.get("title"))
    if not name:
        raise paths.PassnoteError("no session name found: pass --as <name> (use your ListAgents name)", 2)
    other_rooms = [other for other in meta["rooms"] if other != room]
    old_name = meta.get("name")
    renamed = bool(other_rooms and old_name and old_name != name)
    if renamed:
        # One name per session: senders address it, so it must be the same in every room. The new
        # room is in the list so the clash check covers it too, before anything is written.
        if not rooms.rename(sid, other_rooms + [room], name):
            raise paths.PassnoteError(
                f"name {name!r} is in use by a running session in another room; pick another with --as", 2)
    try:
        result = rooms.join(sid, room, name, root, display=display)
    except (paths.PassnoteError, paths.LockBusy):
        if renamed:
            rooms.rename(sid, other_rooms, old_name)  # best effort: back to the one old name
        raise
    sessions.update_meta(sid, lambda current: current.update(name_source=source))
    if pid is not None:
        sessions.record_pid(pid, sid)
    stdout.write(f"joined {render.escape_text(result['display'])} ({room}) as {name}\n"
                 f"root: {render.escape_text(result['root'])}\n"
                 f"members: {', '.join(render.escape_text(member) for member in result['members'])}\n")
    if result["warning"]:
        stdout.write(f"warning: {render.escape_text(result['warning'])}\n")
    try:
        rooms.gc()
    except Exception as exc:  # opportunistic; never fail a join because of gc
        paths.log_error("gc", exc)
    return 0


def cmd_leave(args, stdin, stdout, env):
    sid, meta = _session(env)
    room = _room(args, meta)
    # Not while a hook fire is between reading the room and saving its cursor: it would re-create
    # cursors/<room>.json for a room just left.
    with sessions.session_lock(sid, timeout=LEAVE_LOCK_TIMEOUT):
        left = rooms.leave(sid, room)
    if not left:
        raise paths.PassnoteError(f"not joined to {room}", 3)
    stdout.write(f"left {room}\n")
    return 0


def cmd_rooms(args, stdin, stdout, env):
    sid, meta = _session(env)
    if not meta["rooms"]:
        stdout.write("not joined to any room; run: passnote join\n")
        return 0
    for room in meta["rooms"]:
        rmeta = store.load_meta(room)
        count = len(store.load_members(room))
        display = render.escape_text(rmeta.get("display") or room)
        stdout.write(f"{display} ({room}) · {count} member(s) · root {render.escape_text(rmeta.get('root', '?'))}\n")
    return 0


def cmd_post(args, stdin, stdout, env):
    sid, meta = _session(env)
    room = _room(args, meta)
    return _post(sid, room, stdin.read().rstrip("\n"), args.kind, args.to, args.re_id,
                 args.wake, args.urgent, args.allow_secret_looking, stdout)


def cmd_claim(args, stdin, stdout, env):
    sid, meta = _session(env)
    room = _room(args, meta)
    if args.release:
        if not any(msg["id"] == args.release and msg["kind"] == "claim" for msg in _valid_messages(room)):
            raise paths.PassnoteError(f"no claim {render.escape_text(args.release)} in {room} to release", 2)
        return _post(sid, room, "release", "claim", None, args.release, False, False, False, stdout)
    if not args.what:
        raise paths.PassnoteError('say what you claim: passnote claim "<what>"', 2)
    return _post(sid, room, args.what, "claim", None, None, False, False, False, stdout)


def _post(sid, room, text, kind, to_arg, re_id, wake_flag, urgent, allow_secret, stdout):
    members = _members_or_exit(sid, room)
    cfg = config.load(room)
    if not text.strip():
        raise paths.PassnoteError("empty message: pass the text on stdin", 2)
    # The cap first: a long text is refused for its length (exit 4) without scanning all of it.
    if len(text) > cfg["text_max_chars"]:
        raise paths.PassnoteError(f"text is {len(text)} chars (max {cfg['text_max_chars']}); "
                                  "write it to a file and post the path", 4)
    if not allow_secret:
        hit = trust.looks_secret(text)
        if hit:
            raise paths.PassnoteError(f"the text looks like it contains a secret ({hit}); never post credentials. "
                                      "If this is a false alarm, pass --allow-secret-looking", 2)
    sid_by_name = {info["name"]: member_sid for member_sid, info in members.items()}
    if to_arg:
        to = list(dict.fromkeys(name.strip() for name in to_arg.split(",") if name.strip()))
        unknown = [name for name in to if name not in sid_by_name]
        if unknown or not to:
            raise paths.PassnoteError(
                f"not in {room}: {render.escape_text(', '.join(unknown) or to_arg)} "
                f"(members: {', '.join(render.escape_text(name) for name in sorted(sid_by_name))})", 2)
    else:
        if wake_flag or urgent:
            raise paths.PassnoteError("--wake and --urgent need --to", 2)
        to = "all"
    me = members[sid]
    rec = {"from": me["name"], "sid": sid, "to": to, "kind": kind, "text": text,
           "mode": sessions.recorded_mode(sid) or "unknown"}
    if re_id:
        rec["re"] = re_id
    if wake_flag or urgent:
        rec["wake"] = True
    msg = store.append_message(room, rec, me["alias"])
    stdout.write(f"ok {msg['id']}\n")
    if isinstance(to, list):
        by_id = {m["id"]: m for m in _valid_messages(room)} if re_id else {}
        for name in to:
            target = sid_by_name[name]
            if target == sid or not wake.is_eligible(msg, name, target, by_id, members):
                continue
            reason = wake.held(msg, target, cfg["inbound"])
            if reason:
                decision = "WAIT"
            else:
                decision, reason = wake.decide(room, target, sid, urgent, cfg["wake_breaker"])
            store.append_event(room, {"type": "wake", "decision": decision, "reason": reason, "id": msg["id"],
                                      "from_sid": sid, "to_sid": target, "to": name})
            if decision == "WAKE":
                stdout.write(wake.doorbell_line(name, msg) + "\n")
            else:
                stdout.write(f"WAIT {render.escape_text(name)} {reason}\n")
    return 0


def cmd_read(args, stdin, stdout, env):
    sid, meta = _viewer(env)
    room = _room(args, meta)
    members = store.load_members(room)
    msgs = _valid_messages(room)
    me, held, unrecorded = None, 0, 0
    if sid:
        if sid not in members:
            raise paths.PassnoteError(f"not joined to {room}; run: passnote join", 3)
        me = members[sid]["name"]
        inbound = trust.effective_inbound(config.load(room)["inbound"],
                                          claude_settings.inbound(env.get("CLAUDE_PROJECT_DIR")))
        visible = []
        for msg in msgs:
            if msg["sid"] == sid:  # visibility() skips the reader's own lines; read shows them
                visible.append(msg)
                continue
            verdict, reason = trust.visibility(msg, sid, me, meta.get("permission_mode"), inbound, env)
            if verdict == "hold" and reason == "receiver mode unknown":
                unrecorded += 1
            elif verdict == "hold":
                held += 1
            elif verdict == "deliver":
                visible.append(msg)
        msgs = visible
    if args.id:
        selected = [msg for msg in msgs if msg["id"] == args.id]
        if not selected:
            raise paths.PassnoteError(f"no message {render.escape_text(args.id)} visible to you in {room}", 2)
    elif args.since:
        ids = [msg["id"] for msg in msgs]
        if args.since not in ids:
            raise paths.PassnoteError(f"no message {render.escape_text(args.since)} visible to you in {room}", 2)
        selected = msgs[ids.index(args.since) + 1:]
    else:
        selected = msgs[-args.last:] if args.last > 0 else []
    for msg in selected:
        stdout.write(render.render_line(msg, me, members, render.UNBOUNDED) + "\n")
    if unrecorded:
        stdout.write(f"({unrecorded} message(s) not shown until this session's permission mode is recorded; "
                     "run passnote read again in a separate command)\n")
    if held:
        stdout.write(f"({held} held message(s) not shown; the human can see them with `passnote watch` "
                     "outside Claude Code)\n")
    return 0


def _held(room):
    """{(message id, addressee sid)} the delivery hook held back (hold events)."""
    return {(ev["id"], ev["to_sid"]) for ev in store.read_events(room)
            if ev.get("type") == "hold" and isinstance(ev.get("id"), str) and isinstance(ev.get("to_sid"), str)}


def _folded(room, members, messages=None):
    messages = _valid_messages(room) if messages is None else messages
    return fold.fold(messages, members, {member: cursor.load(member, room) for member in members}, held=_held(room))


def _who_messages(room, members, sid, meta, env):
    """(messages, held, unrecorded) for `who` run inside session `sid`: its output reaches the model,
    so another session's message whose content would be held for this session is left out, whoever
    it is addressed to (spec §9: only the human sees a held message). Its own messages, including
    those from before a /clear, are kept."""
    inbound = trust.effective_inbound(config.load(room)["inbound"],
                                      claude_settings.inbound(env.get("CLAUDE_PROJECT_DIR")))
    kept, held, unrecorded = [], 0, 0
    for msg in _valid_messages(room):
        sender = store.member_for_sid(members, msg["sid"])
        reason = None if sender and sender[0] == sid else trust.content_hold(
            msg, meta.get("permission_mode"), inbound, env)
        if reason == "receiver mode unknown":
            unrecorded += 1
        elif reason:
            held += 1
        else:
            kept.append(msg)
    return kept, held, unrecorded


def _unanswered_line(item):
    waiting = ", ".join(render.escape_text(name) for name in item["waiting_on"])
    return (f"unanswered {render.escape_text(item['id'])} {render.escape_text(item['kind'])} "
            f"from {render.escape_text(item['from'])} → waiting on {waiting}: {render.gist(item['text'], GIST_CHARS)}")


def _session_state(member, now):
    """"warm" | "cold (last active Nm ago)" | "cold (never active)", plus " · gone" for a process that ended."""
    state, age, _ = sessions.warmth(member, now)
    if state == "warm":
        text = "warm"
    elif age is None:
        text = "cold (never active)"
    else:
        text = f"cold (last active {int(age // 60)}m ago)"
    return text + ("" if rooms.is_running(member) else " · gone")


def cmd_who(args, stdin, stdout, env):
    sid, meta = _viewer(env)
    room = _existing_room(_room(args, meta))
    members = store.load_members(room)
    rmeta = store.load_meta(room)
    held, unrecorded = 0, 0
    if sid:
        messages, held, unrecorded = _who_messages(room, members, sid, meta, env)
        state = _folded(room, members, messages)
    else:
        state = _folded(room, members)  # the human's terminal: everything
    now = time.time()
    # Only once this session's mode is recorded: an unrecorded mode would flag every member.
    my_class = trust.mode_class(meta["permission_mode"]) if meta and meta.get("permission_mode") else None
    stdout.write(f"room {render.escape_text(rmeta.get('display') or room)} ({room}) · "
                 f"root {render.escape_text(rmeta.get('root', '?'))}\n")
    for member, info in sorted(members.items(), key=lambda kv: kv[1]["name"]):
        smeta = sessions.load_meta(member)
        mode = smeta.get("permission_mode")
        line = (f"  {render.escape_text(info['name'])} ({render.escape_text(info['alias'])}) · "
                f"{_session_state(member, now)} · mode {render.gist(mode, 40) if mode else 'unknown'}")
        if member == sid:
            line += " · you"
        last_error = smeta.get("last_error")
        if isinstance(last_error, dict) and last_error.get("error"):
            line += f" · last error {render.gist(last_error['error'], 40)}"
        stdout.write(line + "\n")
        if my_class and member != sid and trust.mode_class(mode) != my_class:
            stdout.write("    note: different permission class; messages between you are held (only the human sees "
                         "them) unless the receiver was launched with PASSNOTE_ALLOW_BYPASS=1; doorbells also need "
                         "crossSessionInbound: accept\n")
    for item in state["unanswered"]:
        stdout.write(_unanswered_line(item) + "\n")
    for claim in state["claims"]:
        stdout.write(f"claim {render.escape_text(claim['id'])} {render.escape_text(claim['from'])}: "
                     f"{render.gist(claim['text'], GIST_CHARS)}\n")
    for name, text in sorted(state["status"].items()):
        stdout.write(f"status {render.escape_text(name)}: {render.gist(text, GIST_CHARS)}\n")
    for prop in state["props"]:
        line = (f"prop {render.escape_text(prop['id'])} from {render.escape_text(prop['from'])} · seen by "
                f"{', '.join(render.escape_text(name) for name in prop['seen']) or 'nobody'}")
        if prop["unseen"]:
            line += f" · not yet seen by {', '.join(render.escape_text(name) for name in prop['unseen'])}"
        stdout.write(line + "\n")
    if unrecorded:
        stdout.write(f"({unrecorded} message(s) not shown until this session's permission mode is recorded; "
                     "run passnote who again in a separate command)\n")
    if held:
        stdout.write(f"({held} message(s) from sessions in a different permission class not shown; "
                     "the human can see them with passnote watch outside Claude Code)\n")
    return 0


_KIND_COLORS = {"ask": "31", "err": "31", "nak": "33", "ans": "32", "done": "32", "prop": "36", "claim": "35"}


def _describe_event(ev, members):
    kind = ev.get("type")
    if kind == "wake":
        return f"wake {ev.get('decision')} {ev.get('to')} {ev.get('id')} ({ev.get('reason')})"
    if kind == "hold":
        to_sid = str(ev.get("to_sid", "?"))
        who = (members.get(to_sid) or {}).get("name") or to_sid[:8]
        return f"hold {ev.get('id')} → {who} ({ev.get('reason')})"
    if kind in ("join", "leave", "rename"):
        return f"{kind} {ev.get('name') or str(ev.get('sid', '?'))[:8]}"
    if kind == "carry":
        return f"carry {str(ev.get('from_sid', '?'))[:8]} → {str(ev.get('sid', '?'))[:8]}"
    return str(kind)


def _watch_emit(stdout, display, rec, members, color, is_event):
    stamp = time.strftime("%H:%M:%S", time.localtime(_ts(rec))) if _ts(rec) else "--:--:--"
    if is_event:
        body = f"· {render.gist(_describe_event(rec, members), 200)}"
        code = "2"
    else:
        body = render.render_line(rec, None, members, render.UNBOUNDED)
        code = _KIND_COLORS.get(rec.get("kind"))
    line = f"{stamp} [{render.escape_text(display)}] {body}"
    stdout.write((f"\x1b[{code}m{line}\x1b[0m" if color and code else line) + "\n")


def _ts(rec):
    ts = rec.get("ts")
    return ts if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0 else 0


def _line_start(fh, before) -> int:
    """Offset just after the last newline before byte `before`, i.e. where the line holding the
    window starts. Scans back at most MAX_SKIP bytes, then gives up with 0: read_from's own
    oversized-line handling takes over from the top."""
    end, scanned = before, 0
    while end > 0 and scanned < store.MAX_SKIP:
        begin = max(0, end - 4096)
        fh.seek(begin)
        found = fh.read(end - begin).rfind(b"\n")
        if found >= 0:
            return begin + found + 1
        scanned += end - begin
        end = begin
    return 0


def _snapshot(path):
    """(inode, offset, [whole lines of the last MAX_READ bytes]) from one open file, so the offset
    the follow resumes from is exactly the end of what was shown. A trailing unterminated line is
    left for later; if the window holds no newline at all (one oversized or still-unfinished line)
    there are no lines and the offset is that line's start, where read_from takes over."""
    try:
        with open(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            start = max(0, st.st_size - store.MAX_READ)
            fh.seek(max(0, start - 1))
            data = fh.read(st.st_size - max(0, start - 1))
            lines = data.split(b"\n")
            if start > 0:
                lines = lines[1:]  # a partial first line (or the empty piece after the byte before the window)
            if not lines:
                return st.st_ino, _line_start(fh, start - 1), []
    except OSError:
        return None, 0, []
    tail = lines.pop()  # b"" if the file ends in a newline, else an unfinished line
    return st.st_ino, st.st_size - len(tail), lines


class _Follow:
    """One file followed like `tail -F`: by (inode, offset), restarting at 0 when it is replaced."""

    def __init__(self, path, ino, off):
        self.path, self.ino, self.off = path, ino, off

    def new_lines(self):
        out = []
        try:
            with open(self.path, "rb") as fh:
                st = os.fstat(fh.fileno())  # the file read below, not whatever the path names next
                if st.st_ino != self.ino or st.st_size < self.off:
                    self.ino, self.off = st.st_ino, 0
                while True:
                    lines, self.off = store.read_from_fh(fh, self.path, self.off)
                    if not lines:
                        return out
                    out.extend(raw for _, raw in lines)
        except OSError:
            return out


def _watch_color(stdout, env) -> bool:
    return bool(getattr(stdout, "isatty", lambda: False)()) and not env.get("NO_COLOR") and env.get("TERM") != "dumb"


def _records(lines, is_event):
    recs = [store.parse(raw) for raw in lines]
    return [rec for rec in recs if rec and (is_event or store.valid_message(rec))]


def cmd_watch(args, stdin, stdout, env):
    if env.get("CLAUDE_CODE_SESSION_ID"):
        # Inside a session (even the human's own `!` command) the output lands in the model's
        # context, and watch shows held messages, which only the human may see (spec §9).
        raise paths.PassnoteError("watch shows held messages, so it runs only in a terminal outside "
                                  "Claude Code (see passnote shim)", 2)
    room_list = _all_rooms() if args.all else [_existing_room(_room(args, None))]
    if not room_list:
        stdout.write("(no rooms yet)\n")
        return 0
    color = _watch_color(stdout, env)
    followed = []
    for room in room_list:
        members = store.load_members(room)
        display = store.load_meta(room).get("display") or room
        for item in _folded(room, members)["unanswered"]:
            stdout.write(f"[{render.escape_text(display)}] {_unanswered_line(item)}\n")
        recent = []
        for is_event, path in ((False, store.log_path(room)), (True, store.events_path(room))):
            ino, off, lines = _snapshot(path)
            recs = _records(lines, is_event)
            recent += [(rec, is_event) for rec in (recs[-args.last:] if args.last > 0 else [])]
            followed.append((room, display, is_event, _Follow(path, ino, off)))
        for rec, is_event in sorted(recent, key=lambda pair: _ts(pair[0])):
            _watch_emit(stdout, display, rec, members, color, is_event)
    while not args.once:
        stdout.flush()
        time.sleep(POLL_SECONDS)  # Ctrl-C lands here or in a write: main() turns it into exit 130
        for room, display, is_event, follow in followed:
            recs = _records(follow.new_lines(), is_event)
            if recs:
                members = store.load_members(room)
                for rec in recs:
                    _watch_emit(stdout, display, rec, members, color, is_event)
    return 0


def cmd_gc(args, stdin, stdout, env):
    result = rooms.gc(max_age_days=args.days)
    stdout.write(f"removed {result['sessions']} stale session(s), {result['members']} membership(s), "
                 f"{result['pids']} pid record(s)\n")
    return 0


# What passnote itself puts in PASSNOTE_HOME. PASSNOTE_HOME may be a directory with other things in it
# (even ~), so uninstall deletes exactly these.
HOME_CHILDREN = ("rooms", "sessions", "config.json", "errors.log", "errors.log.1")


def _remove(path, failures) -> None:
    try:
        if os.path.islink(path) or not os.path.isdir(path):
            os.unlink(path)
        else:
            shutil.rmtree(path, onerror=lambda func, failed, exc_info: failures.append((failed, exc_info[1])))
    except FileNotFoundError:
        pass
    except OSError as exc:
        failures.append((path, exc))


def cmd_uninstall(args, stdin, stdout, env):
    if not args.purge:
        raise paths.PassnoteError("pass --purge to delete all passnote data "
                                  "(remove the plugin itself with /plugin uninstall passnote)", 2)
    root = paths.home()
    present = [name for name in HOME_CHILDREN if os.path.lexists(os.path.join(root, name))]
    if not present:
        stdout.write(f"nothing to delete in {root}\nremove the plugin with: /plugin uninstall passnote\n")
        return 0
    if not args.yes:
        stdout.write(f"This deletes passnote's data in {root} ({', '.join(present)}). Type 'purge' to confirm: ")
        stdout.flush()
        if stdin.readline().strip() != "purge":
            raise paths.PassnoteError("not confirmed; nothing was deleted", 2)
    failures = []
    for name in present:
        _remove(os.path.join(root, name), failures)
    try:
        os.rmdir(root)  # only if nothing else is in it
    except OSError:
        pass
    if failures:
        details = "; ".join(f"{render.escape_text(failed)}: {getattr(exc, 'strerror', None) or type(exc).__name__}"
                            for failed, exc in failures)
        raise paths.PassnoteError(f"could not delete {details} (the rest of {root} was purged)", 2)
    stdout.write(f"deleted passnote's data in {root}\nremove the plugin with: /plugin uninstall passnote\n")
    return 0


SHIM_MARKER = "# passnote-shim:"
# The shim is Python (the plugin needs python3 anyway) so that it can read installed_plugins.json
# and sort versions numerically. Resolution order: an installed_plugins.json entry for passnote
# whose installPath exists, else the newest version directory under plugins/cache/*/passnote/
# without an .orphaned_at marker (0.10.0 is newer than 0.9.2; non-numeric names rank last).
SHIM = r'''#!/usr/bin/env python3
# passnote-shim: finds the installed passnote plugin when it runs, so plugin updates keep working.
import glob
import json
import os
import re
import sys


def entry_point(root):
    path = os.path.join(root, "bin", "passnote")
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def entries(node, key=""):
    if isinstance(node, dict):
        if isinstance(node.get("installPath"), str):
            yield key, node
        else:
            for name, value in node.items():
                for found in entries(value, name):
                    yield found
    elif isinstance(node, list):
        for value in node:
            for found in entries(value, key):
                yield found


def installed(plugins):
    try:
        with open(os.path.join(plugins, "installed_plugins.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        for key, entry in entries(data):
            if key == "passnote" or str(key).startswith("passnote@") or entry.get("name") == "passnote":
                found = entry_point(entry["installPath"])
                if found:
                    return found
    except (OSError, ValueError, RecursionError):
        pass
    return None


def version_key(root):
    name = os.path.basename(root)
    if re.match(r"^[0-9]+(\.[0-9]+)*$", name):
        return (1, tuple(int(part) for part in name.split(".")), root)
    return (0, (), root)


def newest(plugins):
    roots = glob.glob(os.path.join(glob.escape(plugins), "cache", "*", "passnote", "*"))
    roots = [root for root in roots
             if entry_point(root) and not os.path.exists(os.path.join(root, ".orphaned_at"))]
    return entry_point(max(roots, key=version_key)) if roots else None


def main():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    plugins = os.path.join(base, "plugins")
    found = installed(plugins) or newest(plugins)
    if not found:
        sys.stderr.write("passnote: plugin not found; install it with /plugin install passnote@<marketplace>\n")
        return 2
    try:
        os.execv(found, [found] + sys.argv[1:])
    except OSError as exc:
        sys.stderr.write("passnote: cannot run %s: %s\n" % (found, exc.strerror or type(exc).__name__))
        return 2


sys.exit(main())
'''


def _is_shim(path) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return SHIM_MARKER in fh.read(4096)
    except OSError:
        return False


def _on_path(directory, env) -> bool:
    real = os.path.realpath(directory)
    return any(entry and os.path.realpath(entry) == real for entry in env.get("PATH", "").split(os.pathsep))


def cmd_shim(args, stdin, stdout, env):
    target = os.path.abspath(os.path.expanduser(args.path))
    directory = os.path.dirname(target)
    if os.path.isdir(target):
        raise paths.PassnoteError(f"{target} is a directory; pass --path <file>", 2)
    if os.path.lexists(target) and not args.force and not _is_shim(target):
        raise paths.PassnoteError(f"{target} exists and is not a passnote shim; pass --force to replace it", 2)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".passnote-shim-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(SHIM)
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    stdout.write(f"installed {target}\n")
    if not _on_path(directory, env):
        stdout.write(f"note: add {directory} to your PATH\n")
    return 0


def cmd_doctor(args, stdin, stdout, env):
    from . import doctor
    return doctor.run(env, stdout)
