"""Rooms and membership: default room, join/leave, aliases, takeover, rename, carry-over, gc (spec §4, §5)."""
from __future__ import annotations

import contextlib
import itertools
import json
import os
import re
import string
import time

from . import cursor, paths, sessions, store, transcript

RENAME_LOCK_TIMEOUT = 1.0


def default_room(cwd):
    # Imported here, not at module top: every hook imports this module, few call this (P1).
    import hashlib
    import subprocess

    root = None
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            root = os.path.dirname(os.path.realpath(out.stdout.strip()))
    except (OSError, subprocess.SubprocessError):
        root = None
    if not root:
        root = os.path.realpath(cwd)
    display = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(root)).strip("-._")[:40] or "room"
    # Not a security use (FIPS builds refuse plain sha1); the algorithm must not change, or rooms rename.
    digest = hashlib.sha1(root.encode("utf-8"), usedforsecurity=False).hexdigest()[:4]
    return f"{display}-{digest}", display, root


def _has_session_dir(sid) -> bool:
    try:
        return os.path.isdir(paths.session_dir(sid))
    except paths.PassnoteError:
        return False


def is_running(sid) -> bool:
    """Whether sid's session is still running (ticket 01): "unknown" counts as running, the
    safe default, so a name clash is an error rather than a silent takeover. A member with no
    session dir (orphaned, e.g. a crash between save_members and save_meta) or a sid that isn't
    a valid session id at all is treated as gone, so its name doesn't stay blocked forever."""
    if not _has_session_dir(sid):
        return False
    return sessions.process_state(sid) != "gone"


def _member_states(room, wanted=lambda sid, info: True):
    """{sid: (had_session_dir, running)} for the room's members that `wanted` selects, computed
    BEFORE taking the room lock (C4): each check can run `ps` (up to 2 s), and every append to
    the room waits on that lock. Read again under the lock with _running_now."""
    return {sid: (_has_session_dir(sid), is_running(sid))
            for sid, info in store.load_members(room).items() if wanted(sid, info)}


def _running_now(sid, states) -> bool:
    """Under the room lock: the state from _member_states, unless the sid wasn't seen then or its
    session dir has appeared since (a join in progress) -- only then check the process now."""
    seen = states.get(sid)
    if seen is None or (not seen[0] and _has_session_dir(sid)):
        return is_running(sid)
    return seen[1]


def _valid_sid(sid) -> bool:
    try:
        paths.check_sid(sid)
        return True
    except paths.PassnoteError:
        return False


def _alias(name, used):
    base = re.sub(r"[^a-z]", "", name.lower()) or "m"
    for size in range(1, len(base) + 1):
        candidate = base[:size]
        if candidate != "w" and candidate not in used:
            return candidate
    for size in itertools.count(1):
        for tail in itertools.product(string.ascii_lowercase, repeat=size):
            candidate = base + "".join(tail)
            if candidate not in used:
                return candidate


def _drop_room_from_session(sid, room):
    if not _has_session_dir(sid):
        return  # nothing to drop; taking the meta lock would recreate the dir

    def drop(meta):
        if room not in meta["rooms"]:
            return False
        meta["rooms"].remove(room)

    sessions.update_meta(sid, drop)


def join(sid, room, name, root, display=None, now=None) -> dict:
    now = time.time() if now is None else now
    paths.check_name(name)
    warning = None
    clashing = _member_states(room, lambda other, info: other != sid and info.get("name") == name)
    with store.room_lock(room):
        members = store.load_members(room)
        meta = store.load_meta(room)
        if not meta:
            meta = {"root": root, "display": display or room, "created_at": now, "aliases_used": []}
        elif meta.get("root") and meta["root"] != root:
            warning = f"this room was created for {meta['root']}; you joined from {root}"
        inherited, taken_over = None, []
        for other, info in list(members.items()):
            if other != sid and info.get("name") == name:
                if _running_now(other, clashing):
                    raise paths.PassnoteError(
                        f"name {name!r} is in use by a running session; pick another with --as", 2)
                if _valid_sid(other):
                    inherited = cursor.load(other, room)
                    # Can raise LockBusy: nothing is removed yet, so a retry still inherits.
                    _drop_room_from_session(other, room)
                    taken_over.append(other)
                del members[other]
        aliases = meta.get("aliases_used")
        used = {a for a in aliases if isinstance(a, str)} if isinstance(aliases, list) else set()
        if sid in members:
            alias = members[sid]["alias"]
        else:
            alias = _alias(name, used)
            used.add(alias)
        earlier = members.get(sid, {}).get("prev_sids")
        members[sid] = {
            "name": name,
            "alias": alias,
            "joined_at": members.get(sid, {}).get("joined_at", now),
            "root": root,
        }
        if earlier:  # a re-join after /clear: its messages from before stay its own
            members[sid]["prev_sids"] = earlier
        meta["aliases_used"] = sorted(used)
        store.save_meta(room, meta)
        store.save_members(room, members)
        if cursor.load(sid, room) is None:
            cursor.save(sid, room, inherited or cursor.at_eof(room))
        for other in taken_over:  # only once the newcomer holds the inherited position
            cursor.remove(other, room)

    def record(smeta):
        if room not in smeta["rooms"]:
            smeta["rooms"].append(room)
        smeta["name"] = name

    sessions.update_meta(sid, record)
    store.append_event(room, {"type": "join", "sid": sid, "name": name})
    return {
        "room": room,
        "display": meta.get("display") or room,
        "root": meta.get("root") or root,
        "members": sorted(info["name"] for info in members.values()),
        "warning": warning,
        "alias": alias,
    }


def leave(sid, room) -> bool:
    with store.room_lock(room):
        members = store.load_members(room)
        info = members.pop(sid, None)
        if info is not None:
            store.save_members(room, members)
    cursor.remove(sid, room)
    _drop_room_from_session(sid, room)
    if info is not None:
        store.append_event(room, {"type": "leave", "sid": sid, "name": info.get("name")})
    return info is not None


def rename(sid, room_list, new_name, now=None) -> bool:
    """Rename sid in every room, or in none: the clash check and the writes happen under all the
    room locks (taken in sorted order), so no one can take the name in between (C2)."""
    paths.check_name(new_name)
    room_list = sorted(set(room_list))
    clashing = {}
    for room in room_list:
        clashing.update(_member_states(room, lambda other, info: other != sid and info.get("name") == new_name))
    try:
        with contextlib.ExitStack() as locks:
            for room in room_list:
                locks.enter_context(store.room_lock(room, timeout=RENAME_LOCK_TIMEOUT))
            members_by_room = {room: store.load_members(room) for room in room_list}
            if any(other != sid and info.get("name") == new_name and _running_now(other, clashing)
                   for members in members_by_room.values() for other, info in members.items()):
                return False
            for room, members in members_by_room.items():
                if sid in members:
                    members[sid]["name"] = new_name
                    store.save_members(room, members)
    except paths.LockBusy:
        return False
    for room in room_list:
        store.append_event(room, {"type": "rename", "sid": sid, "name": new_name})
    return True


def carry_over(old_sid, new_sid) -> None:
    """/clear: move membership, cursors and meta from the old session id to the new one.
    The member keeps the old id in its prev_sids (the last store.MAX_PREV_SIDS), so its claims,
    asks and messages from before the /clear still count as its own (store.member_for_sid).
    Safe to run again after a LockBusy or a kill part-way: a moved room is skipped, and the old
    session (the source of truth until its dir is removed at the end) still lists the rest."""
    import shutil
    old = sessions.load_meta(old_sid)
    if not old["rooms"]:
        return  # nothing to carry (already done): never blank the new session's rooms
    # First, so the moved member is never briefly "gone" (is_running is False without the dir).
    paths.makedirs(paths.session_dir(new_sid))
    for room in old["rooms"]:
        with store.room_lock(room, timeout=2.0):
            members = store.load_members(room)
            if old_sid in members:
                info = members.pop(old_sid)
                earlier = [sid for sid in info.get("prev_sids", ()) if sid not in (old_sid, new_sid)] + [old_sid]
                info["prev_sids"] = store.prev_sids(earlier)
                members[new_sid] = info
                store.save_members(room, members)
        cur = cursor.load(old_sid, room)
        if cur:
            cursor.save(new_sid, room, cur)
        store.append_event(room, {"type": "carry", "from_sid": old_sid, "sid": new_sid})

    def inherit(new):
        for key in ("name", "name_source", "permission_mode", "ttl_seconds", "title"):
            new[key] = old.get(key)
        if old.get("pid"):  # the pair travels together
            new["pid"], new["pid_started_at"] = old.get("pid"), old.get("pid_started_at")
        new["rooms"] = list(old["rooms"])

    sessions.update_meta(new_sid, inherit)
    _carry_emit(old_sid, new_sid, old)
    shutil.rmtree(paths.session_dir(old_sid), ignore_errors=True)


def _carry_emit(old_sid, new_sid, old_meta) -> None:
    """Move the delivery hook's emit state. Overflow and ahead refs are already behind the cursor:
    without them a /clear would lose those messages. Emitted refs are checked against the OLD
    transcript (the new one is a different file): the unconfirmed ones are carried as overflow,
    to be rendered again; an unreadable transcript confirms nothing."""
    old_emit, new_emit = sessions.load_emit(old_sid), sessions.load_emit(new_sid)
    if not any(old_emit.values()):
        return
    emitted = [ref for ref in old_emit["emitted"] if isinstance(ref, dict)]
    path = old_meta.get("transcript_path")
    missing = transcript.unconfirmed(path if isinstance(path, str) else None, emitted)
    # Carried as overflow, to be rendered (and its line recorded) again.
    unconfirmed = [{key: value for key, value in ref.items() if key != "line"}
                   for ref in (emitted if missing is None else missing)]

    def merged(*lists):
        out, seen = [], set()
        for ref in (ref for refs in lists for ref in refs):
            key = json.dumps(ref, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                out.append(ref)
        return out

    sessions.save_emit(new_sid, new_emit["emitted"], merged(new_emit["overflow"], old_emit["overflow"], unconfirmed),
                       merged(new_emit["ahead"], old_emit["ahead"]))


def _sweep_orphan_members(result) -> None:
    """gc, second pass (ticket 01 §4): reclaim members whose sid has no session dir, or isn't a
    valid session id at all (e.g. a crash between save_members and save_meta left the member in
    a room the pruned session's meta never listed, or a corrupt key), so an orphan can't block a
    name forever. Such a member can't be resumed, so it goes at once. A member with a session dir
    is left to the first pass's age rule, even when its process is gone. Decided under the room
    lock (a cheap directory test, no `ps`), so a join that finished meanwhile keeps its member."""
    rooms_root = os.path.join(paths.home(), "rooms")
    if not os.path.isdir(rooms_root):
        return
    for room in os.listdir(rooms_root):
        if not paths.valid_name(room):
            continue
        with store.room_lock(room):
            members = store.load_members(room)
            stale = {sid: members[sid].get("name") for sid in members if not _has_session_dir(sid)}
            for sid in stale:
                del members[sid]
            if stale:
                store.save_members(room, members)
        for sid in stale:
            try:
                cursor.remove(sid, room)
                _drop_room_from_session(sid, room)
            except paths.PassnoteError:
                pass
            store.append_event(room, {"type": "leave", "sid": sid, "name": stale[sid]})
            result["members"] += 1


def _listdir(path):
    try:
        return os.listdir(path)
    except FileNotFoundError:
        return []


def _unlink(path) -> bool:
    """Remove path; False if a concurrent gc got there first (C3)."""
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


def gc(max_age_days=7, now=None) -> dict:
    """Prune sessions, their memberships and dead by-pid records. A session goes only when its
    process is gone or unknown AND it has been inactive for more than max_age_days; a running one
    is never pruned, however old. Gone alone is not enough: a closed session can be resumed with
    the same id (spec §5) and must still be a member then (decision 2026-10-03, amending ticket
    01 §4). Taking over a gone member's name stays immediate (join). Then orphan members (no
    session dir) are swept at once."""
    import shutil

    now = time.time() if now is None else now
    limit = max_age_days * 86400
    result = {"sessions": 0, "members": 0, "pids": 0}
    root = os.path.join(paths.home(), "sessions")
    if os.path.isdir(root):
        for entry in os.listdir(root):
            if entry == "by-pid":
                for name in _listdir(os.path.join(root, entry)):
                    pid = sessions.parse_pid(name.split(".")[0])
                    if pid is not None and not sessions.pid_alive(pid) and _unlink(os.path.join(root, entry, name)):
                        result["pids"] += 1
                continue
            try:
                sid = paths.check_sid(entry)
            except paths.PassnoteError:
                continue
            if sessions.process_state(sid) == "running":
                continue
            age = sessions.active_age(sid, now)  # gone or unknown: pruned only once idle past the limit
            if age is None:
                try:
                    age = now - os.stat(os.path.join(root, entry)).st_mtime
                except FileNotFoundError:  # a concurrent gc or carry_over removed it
                    continue
            if age <= limit:
                continue
            for room in sessions.load_meta(sid)["rooms"]:
                if leave(sid, room):
                    result["members"] += 1
            shutil.rmtree(os.path.join(root, entry), ignore_errors=True)
            result["sessions"] += 1
    _sweep_orphan_members(result)
    return result
