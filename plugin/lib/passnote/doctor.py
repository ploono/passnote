"""`passnote doctor`: environment checks, each with a fix line (spec §11)."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time

from . import claude_settings, paths, render, sessions, store, trust

MIN_CLAUDE = (2, 1, 283)
MIN_CLAUDE_TEXT = ".".join(map(str, MIN_CLAUDE))
OK, WARN, FAIL = "ok", "warn", "fail"
_MARKS = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}
ERROR_WINDOW = 86400  # seconds of errors.log that doctor reports
README_SNIPPET = '{"sandbox":{"enabled":true,"filesystem":{"allowWrite":["~/.local/state/passnote"]}}}'


def claude_version(runner=subprocess.run):
    try:
        out = runner(["claude", "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", out.stdout or "")
    return tuple(int(part) for part in match.groups()) if match else None


def _recent_errors(root, now):
    found = []
    try:
        with open(os.path.join(root, "errors.log"), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or not isinstance(rec.get("ts"), (int, float)):
                    continue
                if now - rec["ts"] <= ERROR_WINDOW:
                    detail = ": ".join(str(rec[key]) for key in ("where", "error", "note") if rec.get(key))
                    found.append(render.escape_text(detail or "error"))
    except OSError:
        pass
    return found


def _sandbox_fix(root):
    fix = f"add {root} to sandbox.filesystem.allowWrite in your Claude Code settings"
    if root == os.path.join(os.path.expanduser("~"), ".local", "state", "passnote"):
        fix += f", for example {README_SNIPPET}"
    return fix


def _version_check(runner, which):
    if which("claude") is None:
        return (WARN, "claude is not on PATH, so its version is unknown", "make sure `claude` is on PATH")
    version = claude_version(runner)
    if version is None:
        return (WARN, "Claude Code version unknown",
                f"run `claude --version`; passnote needs {MIN_CLAUDE_TEXT} or newer")
    text = ".".join(map(str, version))
    if version < MIN_CLAUDE:
        return (FAIL, f"Claude Code {text} is older than {MIN_CLAUDE_TEXT}", "run: claude update")
    return (OK, f"Claude Code {text}", "")


def _storage_checks(results):
    """Appends the storage lines; returns the root, or None when storage is unusable."""
    try:
        root = paths.ensure_home()
    except paths.PassnoteError as exc:
        results.append((FAIL, str(exc),
                        "make the directory yours and private (mode 700), or point PASSNOTE_HOME elsewhere"))
        return None
    except OSError:
        results.append((FAIL, f"cannot create or use storage {paths.home()} from this shell",
                        _sandbox_fix(paths.home())))
        return None
    results.append((OK, f"storage {root} is private", ""))
    try:
        probe = os.path.join(root, f".doctor-{os.getpid()}-{os.urandom(4).hex()}")
        os.close(os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        os.unlink(probe)
    except OSError:
        results.append((FAIL, "cannot write to storage from this shell (sandbox?)", _sandbox_fix(root)))
        return None
    results.append((OK, "storage is writable from this shell", ""))
    return root


def _session_checks(results, env, now):
    sid_text = env.get("CLAUDE_CODE_SESSION_ID")
    if not sid_text:
        return
    try:
        sid = paths.check_sid(sid_text)
    except paths.PassnoteError:
        results.append((WARN, "CLAUDE_CODE_SESSION_ID is not a valid session id",
                        "run passnote from inside a Claude Code session"))
        return
    meta = sessions.load_meta(sid)
    if not meta["rooms"]:
        results.append((WARN, "this session has not joined a room", "run: passnote join"))
        return
    state, _, _ = sessions.warmth(sid, now)
    if state == "warm":
        results.append((OK, "hook fired in this session", ""))
    else:
        results.append((WARN, "the passnote hook has not fired in this session recently",
                        "check that /hooks lists passnote; restart the session after installing"))
    mine = trust.mode_class(meta.get("permission_mode"))
    for room in meta["rooms"]:
        for other, info in sorted(store.load_members(room).items()):
            if other != sid and trust.mode_class(sessions.recorded_mode(other)) != mine:
                name = render.escape_text(info["name"])
                results.append((WARN, f"{name} in {room} is in a different permission class",
                                "messages between you are held, and only the human sees a held message, not "
                                "the model, unless the receiver was launched with PASSNOTE_ALLOW_BYPASS=1; "
                                "doorbells also need crossSessionInbound: accept"))


def checks(env, runner=subprocess.run, now=None, which=None):
    now = time.time() if now is None else now
    which = shutil.which if which is None else which
    results = [_version_check(runner, which)]
    py = sys.version_info
    results.append((OK if py >= (3, 9) else FAIL, f"python {py.major}.{py.minor}", "install Python 3.9+ as python3"))
    system = platform.system()
    results.append((OK if system in ("Darwin", "Linux") else FAIL, f"platform {system}",
                    "passnote supports macOS and Linux"))
    settings = claude_settings.merged(env.get("CLAUDE_PROJECT_DIR"))
    if settings.get("disableAllHooks"):
        results.append((FAIL, "disableAllHooks is set", "remove disableAllHooks from your Claude Code settings"))
    if settings.get("allowManagedHooksOnly"):
        results.append((FAIL, "allowManagedHooksOnly is set, so plugin hooks will not run", "ask your admin"))
    root = _storage_checks(results)
    if root is None:
        return results
    _session_checks(results, env, now)
    inbound = settings.get("crossSessionInbound")
    if inbound in ("accept", "auto", "hold", "refuse"):
        results.append((OK, f"crossSessionInbound = {inbound}", ""))
    elif inbound is not None:
        results.append((WARN, "crossSessionInbound has an unrecognized value", "use accept, hold or refuse"))
    recent = _recent_errors(root, now)
    if recent:
        results.append((WARN, f"{len(recent)} error(s) in the last 24h, latest: {recent[-1]}",
                        f"see {os.path.join(root, 'errors.log')}"))
    return results


def run(env, out, runner=subprocess.run, which=None) -> int:
    results = checks(env, runner, which=which)
    for status, label, fix in results:
        out.write(f"{_MARKS[status]} {label}\n")
        if fix and status != OK:
            out.write(f"     fix: {fix}\n")
    return 1 if any(status == FAIL for status, _, _ in results) else 0
