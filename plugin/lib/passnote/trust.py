"""Permission classes, inbound holds and the secret guard (spec §9).

The hold mirrors Claude Code's native rule for SendMessage (spike A7): when the sender and
receiver are in different permission classes, the message is held, in either direction.
Only PASSNOTE_ALLOW_BYPASS=1 in the receiver's launch environment lifts it; config can only tighten.
"""
from __future__ import annotations

import re

from . import config, sessions

PROMPTING = frozenset({"default", "acceptEdits", "plan"})
SECRET_PATTERNS = (
    ("private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("API key", re.compile(r"\bsk(?:_live_|_test_|-)[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("GitHub PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("credential assignment",
     re.compile(r"\b[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*['\"]?\s*[=:]\s*['\"]?[^\s'\"]{16,}['\"]?")),
)


def mode_class(mode) -> str:
    return "prompting" if isinstance(mode, str) and mode in PROMPTING else "nonprompting"


def hold_reason(sender_mode, receiver_mode, env, inbound="auto"):
    inbound = config.strictest(inbound)
    if inbound == "refuse":
        return "refuse"
    if inbound == "hold":
        return "inbound=hold"
    if _allow_bypass(env):
        return None
    if mode_class(sender_mode) != mode_class(receiver_mode):
        return "permission-mode mismatch"
    return None


def effective_inbound(*values) -> str:
    return config.strictest(*values)


def _allow_bypass(env) -> bool:
    return str(env.get("PASSNOTE_ALLOW_BYPASS", "")) == "1"


def sender_mode(msg):
    """The mode `post` stamped; a post stamped "unknown" (run in the same shell command as `join`,
    before any hook recorded the mode) or unstamped falls back to the sender's recorded mode."""
    mode = msg.get("mode")
    if mode is None or mode == "unknown":
        return sessions.recorded_mode(msg.get("sid"))
    return mode


def addressed(msg, me) -> bool:
    to = msg.get("to")
    return to == "all" or (isinstance(to, list) and me is not None and me in to)


def visibility(msg, sid, me, receiver_mode, inbound, env):
    """Whether the session `sid`, named `me`, gets `msg` (a store.valid_message):
    ("skip", None) for its own lines, `status` lines and lines not addressed to it;
    ("hold", reason) when inbound refuses or holds, the permission classes differ, or the
    receiver's mode is unknown (fail closed); else ("deliver", None).
    The delivery hook and `read` both decide through this, so they never disagree (M3/D2).
    `env` is the receiver process's own environment, never values from a log or config file."""
    if msg.get("sid") == sid or msg.get("kind") == "status" or not addressed(msg, me):
        return "skip", None
    reason = hold_reason(sender_mode(msg), receiver_mode, env, inbound)
    if reason in ("refuse", "inbound=hold"):
        return "hold", reason
    if not (isinstance(receiver_mode, str) and receiver_mode) and not _allow_bypass(env):
        return "hold", "receiver mode unknown"
    return ("hold", reason) if reason else ("deliver", None)


def looks_secret(text):
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text or ""):
            return label
    return None
