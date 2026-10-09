"""Session records: meta, activity, by-pid map, emit state and names (spec §5, §7)."""
from __future__ import annotations

import json
import os
import re
import sys
import time

from . import claude_settings, paths

META_DEFAULT = {
    "name": None,
    "name_source": None,
    "title": None,
    "permission_mode": None,
    "transcript_path": None,
    "rooms": [],
    "ttl_seconds": None,
    "last_error": None,
    "pid": None,
    "pid_started_at": None,
}
# pid_t is a signed 32-bit int on macOS and Linux.
PID_MAX = 2 ** 31 - 1
# A meta read-modify-write takes milliseconds; waiting longer means something is wrong.
META_LOCK_TIMEOUT = 1.0
PROC = "/proc"
# Prompt-cache lifetime assumed for a session whose own ttl_seconds is not recorded yet.
DEFAULT_TTL = 3600
_LINUX = sys.platform.startswith("linux")


def meta_path(sid):
    return os.path.join(paths.session_dir(sid), "meta.json")


def load_meta(sid) -> dict:
    meta = {key: (list(value) if isinstance(value, list) else value) for key, value in META_DEFAULT.items()}
    stored = paths.read_json(meta_path(sid), {})
    if isinstance(stored, dict):
        meta.update(stored)
    # A string here would iterate as one-letter room names; keep only a list of valid names.
    rooms = meta.get("rooms")
    meta["rooms"] = [room for room in rooms if paths.valid_name(room)] if isinstance(rooms, list) else []
    return meta


def save_meta(sid, meta) -> None:
    paths.atomic_write_json(meta_path(sid), meta)
    marker = os.path.join(paths.session_dir(sid), "joined")
    if meta.get("rooms"):
        if not os.path.exists(marker):
            os.close(os.open(marker, os.O_WRONLY | os.O_CREAT, 0o600))
    else:
        try:
            os.unlink(marker)
        except FileNotFoundError:
            pass


def meta_lock(sid):
    """Serializes read-modify-writes of the session's meta.json (C1). A dedicated file: never
    the delivery `.lock`, which the hook holds non-blocking around read -> emit -> advance."""
    return paths.FileLock(os.path.join(paths.session_dir(sid), ".meta.lock"), timeout=META_LOCK_TIMEOUT)


def update_meta(sid, fn) -> dict:
    """load -> fn(meta) -> save under the session's meta lock, so concurrent writers (this
    session's hooks, join, write_by_pid, another session's takeover, carry_over) never lose
    each other's change. fn edits meta in place; if it returns False nothing is saved.
    Returns the meta. Raises paths.LockBusy if the lock can't be had within META_LOCK_TIMEOUT."""
    with meta_lock(sid):
        meta = load_meta(sid)
        if fn(meta) is not False:
            save_meta(sid, meta)
    return meta


def session_lock(sid, timeout=None):
    """The delivery lock. The hook takes it non-blocking; a CLI command that must not interleave
    with a hook fire (leave) passes a short `timeout` to wait for it instead."""
    return paths.FileLock(os.path.join(paths.session_dir(sid), ".lock"), blocking=False, timeout=timeout)


def recorded_mode(sid):
    """The permission mode this session's own hooks recorded. Used when a post was stamped 'unknown'
    because it ran in the same shell command as `join`, before any hook had recorded the mode."""
    try:
        return load_meta(paths.check_sid(sid)).get("permission_mode")
    except paths.PassnoteError:
        return None


def touch_active(sid, now=None) -> None:
    active = os.path.join(paths.makedirs(paths.session_dir(sid)), "active")
    if not os.path.exists(active):
        os.close(os.open(active, os.O_WRONLY | os.O_CREAT, 0o600))
    os.utime(active, None if now is None else (now, now))


def active_age(sid, now=None):
    try:
        mtime = os.stat(os.path.join(paths.session_dir(sid), "active")).st_mtime
    except FileNotFoundError:
        return None
    return max(0.0, (time.time() if now is None else now) - mtime)


def warmth(sid, now=None):
    """(state, age, ttl): "warm" if the session was active within its prompt-cache TTL, else
    "cold" (never active included). age is None when it was never active. The one place
    wake decisions, `who` and `doctor` agree on what warm means."""
    age = active_age(sid, now)
    ttl = load_meta(sid).get("ttl_seconds") or DEFAULT_TTL
    return ("warm" if age is not None and age <= ttl else "cold"), age, ttl


def current_sid(env) -> str:
    sid = env.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        raise paths.PassnoteError("not inside a Claude Code session (CLAUDE_CODE_SESSION_ID is not set)", 2)
    return paths.check_sid(sid)


def parse_pid(value):
    """A usable pid (int, or a string of ASCII digits as in CLAUDE_PID) or None. Rejects bool,
    pid <= 0 (os.kill(0, 0) signals our own process group, os.kill(-1, 0) every process, so
    both look "alive") and values beyond pid_t."""
    if isinstance(value, str):
        value = paths.ascii_int(value)
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= PID_MAX:
        return value
    return None


def pid_started_at(pid):
    """An opaque token for when `pid` started, compared for equality to detect pid reuse (spec
    §7). It must stay the same for the same process across calls and callers:
    - Linux: field 22 of /proc/<pid>/stat (no subprocess, and no procps needed);
    - macOS, or Linux without /proc: `ps`'s lstart, pinned to LC_ALL=C TZ=UTC so callers under
      different TZ/LC_* still compare equal."""
    pid = parse_pid(pid)
    if pid is None:
        return None
    if _LINUX and os.path.exists(os.path.join(PROC, "self", "stat")):
        return _proc_started_at(pid)
    return _ps_started_at(pid)


def _proc_started_at(pid):
    """Start time in clock ticks since boot, qualified by the boot id so the token can't repeat
    after a reboot. None if the process doesn't exist or its stat line is garbled."""
    try:
        with open(os.path.join(PROC, str(pid), "stat"), "rb") as fh:
            stat = fh.read()
    except OSError:
        return None
    if b")" not in stat:
        return None
    # Field 2, comm, is in parentheses and may itself hold spaces or ")": split after the last ")".
    # What follows starts at field 3 (state), so field 22 (starttime) is index 19.
    fields = stat.rpartition(b")")[2].split()
    if len(fields) < 20 or not fields[19].isdigit():
        return None
    try:
        with open(os.path.join(PROC, "sys", "kernel", "random", "boot_id"), "rb") as fh:
            boot = fh.read().strip().decode("ascii", "replace")
    except OSError:
        boot = ""
    return f"{boot}+{fields[19].decode('ascii')}"


def _ps_started_at(pid):
    import subprocess  # imported here: it costs every hook several ms at import time (P1)

    env = dict(os.environ, LC_ALL="C", TZ="UTC")
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    started = out.stdout.strip()
    return started or None


def pid_alive(pid) -> bool:
    pid = parse_pid(pid)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_state(sid) -> str:
    """"running" | "gone" | "unknown" for the session's recorded process (ticket 01). A missing
    or corrupt pid (wrong type, <= 0) is "unknown", never a pid to probe."""
    meta = load_meta(sid)
    pid = parse_pid(meta.get("pid"))
    if pid is None:
        return "unknown"
    if not pid_alive(pid):
        return "gone"
    recorded = meta.get("pid_started_at")
    if recorded:
        current = pid_started_at(pid)
        if current is not None and current != recorded:
            return "gone"
        return "running"
    registry_path = os.path.join(claude_settings.claude_home(), "sessions", f"{pid}.json")
    return "running" if os.path.exists(registry_path) else "gone"


def by_pid_path(pid):
    parsed = parse_pid(pid)
    if parsed is None:
        raise paths.PassnoteError(f"invalid pid {pid!r}", 2)
    return os.path.join(paths.home(), "sessions", "by-pid", f"{parsed}.json")


def write_by_pid(pid, record) -> None:
    """Record which session runs as `pid`. An invalid pid (e.g. a garbled CLAUDE_PID) is ignored."""
    pid = parse_pid(pid)
    if pid is None:
        return
    paths.atomic_write_json(by_pid_path(pid), record)
    try:
        sid = paths.check_sid(record.get("sid"))
    except paths.PassnoteError:
        return

    def mirror(meta):
        meta["pid"] = pid
        meta["pid_started_at"] = record.get("pid_started_at")

    update_meta(sid, mirror)


def record_pid(pid, sid, prev_sid=None) -> None:
    """Record `sid` as the session running as `pid`, with the process's start token. prev_sid
    (SessionEnd(clear)) names the session a following SessionStart(clear) takes over from."""
    record = {"sid": sid, "pid_started_at": pid_started_at(pid)}
    if prev_sid:
        record["prev_sid"] = prev_sid
    write_by_pid(pid, record)


def read_by_pid(pid):
    if parse_pid(pid) is None:
        return None
    record = paths.read_json(by_pid_path(pid))
    return record if isinstance(record, dict) else None


def registry_name(pid):
    """The ListAgents name, only when the user set it. The file is undocumented: any error → None."""
    try:
        with open(os.path.join(claude_settings.claude_home(), "sessions", f"{int(pid)}.json"), encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(record, dict) or record.get("nameSource") != "user":
        return None
    name = record.get("name")
    if paths.valid_name(name) and name.lower() not in paths.RESERVED_NAMES:
        return name
    return None


def sanitize_title(title):
    if not isinstance(title, str):
        return None
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-._")[:64].strip("-._")
    if not paths.valid_name(cleaned) or cleaned.lower() in paths.RESERVED_NAMES:
        return None
    return cleaned


def resolve_name(as_name, pid, title):
    if as_name:
        return paths.check_name(as_name), "as"
    if pid:
        name = registry_name(pid)
        if name:
            return name, "registry"
    name = sanitize_title(title)
    if name:
        return name, "title"
    return None, None


def _emit_path(sid):
    return os.path.join(paths.session_dir(sid), "emit.json")


def load_emit(sid) -> dict:
    """The delivery hook's state between fires: refs it emitted last fire (to confirm against the
    transcript), refs that overflowed (rendered next fire), and refs it took ahead of its cursor
    (addressed asks found past a full room share; the cursor skips them when it gets there),
    pending seen receipts (this session's addressed asks and props not yet reported seen), and
    delivery evidence (the asks and props addressed to this session by name that it delivered to
    its model: the only thing a sender's seen receipt trusts)."""
    state = paths.read_json(_emit_path(sid), {})
    if not isinstance(state, dict):
        state = {}
    # Another session's hook reads this too (seen receipts): a value that isn't a list counts as empty.
    return {key: list(state[key]) if isinstance(state.get(key), list) else []
            for key in ("emitted", "overflow", "ahead", "receipts", "delivered")}


def save_emit(sid, emitted, overflow, ahead=(), receipts=(), delivered=()) -> None:
    paths.atomic_write_json(_emit_path(sid), {"emitted": list(emitted), "overflow": list(overflow),
                                              "ahead": list(ahead), "receipts": list(receipts),
                                              "delivered": list(delivered)})
