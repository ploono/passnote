"""Storage root, hardening, validation, atomic JSON, locks and the error log (spec §4)."""
from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import time

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# The canonical (str(uuid.UUID)) form. Matching it directly instead of importing uuid keeps the
# hook cheap: on Linux CPython <= 3.11, `import uuid` pulls in platform and subprocess.
SID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
RESERVED_NAMES = frozenset({"all", "user", "human", "system", "claude", "assistant"})
ERROR_LOG_MAX = 256 * 1024


class PassnoteError(Exception):
    """A user-facing error carrying the CLI exit code."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


class LockBusy(Exception):
    """A non-blocking or timed lock could not be taken."""


def _absolute_env(var):
    """$var with ~ expanded, or None when unset or relative. Hooks run in many cwds, so a relative
    root would give each project its own storage; the XDG spec says to ignore relative values."""
    value = os.path.expanduser(os.environ.get(var) or "")
    return os.path.normpath(value) if os.path.isabs(value) else None


def home() -> str:
    explicit = _absolute_env("PASSNOTE_HOME")
    if explicit:
        return explicit
    state = _absolute_env("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(state, "passnote")


def _check_home(root) -> None:
    st = os.stat(root)
    if st.st_uid != os.getuid():
        raise PassnoteError(f"PASSNOTE_HOME {root} is not owned by you", 2)
    if st.st_mode & 0o022:
        raise PassnoteError(f"PASSNOTE_HOME {root} is writable by group or others; run: chmod 700 {root}", 2)


def ensure_home() -> str:
    os.umask(0o077)
    root = home()
    os.makedirs(root, mode=0o700, exist_ok=True)
    _check_home(root)
    return root


def check_home() -> None:
    """ensure_home's refusals and umask, without creating anything: for the read-only commands,
    which must not trust (or show) content from a home that others can write to."""
    os.umask(0o077)
    root = home()
    if os.path.isdir(root):
        _check_home(root)


def ascii_int(text):
    """int(text) for a short string of ASCII digits, else None. str.isdigit() alone accepts
    "\u00b2" (int() then raises) and other scripts' digits, and int() refuses > 4300 digits."""
    if isinstance(text, str) and 0 < len(text) <= 18 and text.isascii() and text.isdigit():
        return int(text)
    return None


def valid_name(name) -> bool:
    return isinstance(name, str) and bool(NAME_RE.fullmatch(name)) and name not in (".", "..")


def valid_member_name(name) -> bool:
    """What check_name(name) accepts, as a bool: a valid name that isn't reserved. Also the rule
    for a thread name (#25)."""
    return valid_name(name) and name.lower() not in RESERVED_NAMES


def check_name(name, *, member: bool = True) -> str:
    if not valid_name(name):
        raise PassnoteError(f"invalid name {name!r}: use 1-64 of A-Z a-z 0-9 . _ -", 2)
    if member and name.lower() in RESERVED_NAMES:
        raise PassnoteError(f"name {name!r} is reserved", 2)
    return name


def check_sid(sid) -> str:
    """The session id in canonical lower-case 8-4-4-4-12 form. Accepts exactly what uuid.UUID
    parses AND whose str() equals the input lower-cased: no braces, urn: prefix or missing dashes."""
    canonical = str(sid).lower() if sid is not None else ""
    if not SID_RE.fullmatch(canonical):
        raise PassnoteError(f"invalid session id {sid!r}", 2)
    return canonical


def room_dir(room: str) -> str:
    check_name(room, member=False)
    return os.path.join(home(), "rooms", room)


def session_dir(sid: str) -> str:
    return os.path.join(home(), "sessions", check_sid(sid))


def makedirs(path: str) -> str:
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def atomic_write_json(path: str, obj) -> None:
    directory = makedirs(os.path.dirname(path))
    # A fresh name created with O_EXCL inside our own 0700 directory: what tempfile.mkstemp does,
    # without importing tempfile, which costs every hook several ms (P1).
    tmp = os.path.join(directory, f".tmp-{os.getpid()}-{os.urandom(6).hex()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=True, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, NotADirectoryError, ValueError, RecursionError):  # ValueError covers UnicodeDecodeError
        return default


class FileLock:
    """flock on a dedicated file; the kernel releases it if the process dies."""

    def __init__(self, path: str, blocking: bool = True, timeout: float | None = None):
        self.path = path
        self.blocking = blocking
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        makedirs(os.path.dirname(self.path))
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        flags = fcntl.LOCK_EX
        if not self.blocking or deadline is not None:
            flags |= fcntl.LOCK_NB
        while True:
            try:
                fcntl.flock(self.fd, flags)
                return self
            except OSError as exc:
                busy = exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK)
                if busy and deadline is not None and time.monotonic() < deadline:
                    time.sleep(0.02)
                    continue
                os.close(self.fd)
                self.fd = None
                if busy:
                    raise LockBusy(self.path) from None
                raise

    def __exit__(self, *exc):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
        return False


def log_error(where: str, exc: BaseException | None = None, note: str | None = None) -> None:
    """Append one line to errors.log. Never includes stdin or message text; never raises.

    Only the exception's type (plus errno/strerror for an OSError) is recorded, never str(exc):
    that can echo caller data (int() repeats its input, PassnoteError carries names, OSError
    carries file names). `note` is for fixed notices: callers pass a code constant only."""
    try:
        path = os.path.join(home(), "errors.log")
        if os.path.exists(path) and os.path.getsize(path) > ERROR_LOG_MAX:
            os.replace(path, path + ".1")
        record = {"ts": round(time.time(), 3), "where": where}
        if exc is not None:
            record["error"] = type(exc).__name__
        if note is not None:
            record["note"] = note
        if isinstance(exc, OSError) and exc.errno is not None:
            record["errno"] = exc.errno
            record["strerror"] = exc.strerror
        line = json.dumps(record) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass
