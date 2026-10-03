"""Wake decisions for posts (spec §8): eligibility, holds, cache TTL, warm/cold, breaker, doorbell."""
from __future__ import annotations

import time

from . import paths, render, rooms, sessions, store, trust

ELIGIBLE_KINDS = ("ask", "err")
REPLY_KINDS = ("ans", "nak", "done")
DOORBELL_GIST_CHARS = 80


def parse_ttl(value):
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("5m", "300s"):
        return 300
    if text in ("1h", "60m", "3600s"):
        return 3600
    return paths.ascii_int(text) or None


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def transcript_bucket(path):
    """Cache TTL bucket of the most recent main-thread API call in a transcript."""
    if not path:
        return None
    for raw in reversed(store.tail_lines(path)):
        if b"cache_creation" not in raw:
            continue
        rec = store.parse(raw)
        if rec is None or rec.get("isSidechain"):
            continue
        usage = ((rec.get("message") or {}).get("usage") or {}).get("cache_creation") or {}
        if usage.get("ephemeral_1h_input_tokens"):
            return "ephemeral_1h"
        if usage.get("ephemeral_5m_input_tokens"):
            return "ephemeral_5m"
    return None


def resolve_ttl(env, settings_ttl=None, bucket=None, override=None) -> int:
    if override:
        return int(override)
    if _truthy(env.get("FORCE_PROMPT_CACHING_5M")):
        return 300
    for candidate in (env.get("CLAUDE_CODE_PROMPT_CACHE_TTL"), settings_ttl):
        ttl = parse_ttl(candidate)
        if ttl:
            return ttl
    if _truthy(env.get("ENABLE_PROMPT_CACHING_1H")):
        return 3600
    if bucket == "ephemeral_1h":
        return 3600
    if bucket == "ephemeral_5m":
        return 300
    return 300 if env.get("ANTHROPIC_API_KEY") else 3600


def is_eligible(msg, name, target_sid, by_id, members=None) -> bool:
    """Whether msg may wake its addressee `name` (session target_sid). A reply wakes only the
    author of the ask or prop it answers: posted by target_sid, or by an earlier session id of
    that member in `members` (before a /clear)."""
    to = msg.get("to")
    if not isinstance(to, list) or name not in to:
        return False
    if msg.get("kind") in ELIGIBLE_KINDS or msg.get("wake"):
        return True
    if msg.get("kind") in REPLY_KINDS and msg.get("re"):
        target = by_id.get(msg["re"])
        if not target or target.get("kind") not in ("ask", "prop"):
            return False
        author = store.member_for_sid(members, target.get("sid"))
        return target.get("sid") == target_sid or (author is not None and author[0] == target_sid)
    return False


# trust.content_hold's reasons, as the sender's WAIT line words them.
_HELD_WORDS = {
    "permission-mode mismatch": "different permission class",
    "receiver mode unknown": "its permission mode is not recorded yet",
    "refuse": "inbound=refuse",
}


def held(msg, target_sid, inbound):
    """The WAIT reason when `msg` would be held from the session target_sid, else None. A doorbell
    carries a gist of the text, and a receiver with Claude Code's crossSessionInbound: accept would
    show it to the model, so a held post never rings one (spec §9: only the human sees a held
    message). Decided with what the sender can know: the post's stamped mode against the
    receiver's recorded mode (unrecorded holds, fail closed) and the room's or global inbound.
    The receiver's own settings and env are invisible here, so PASSNOTE_ALLOW_BYPASS is not
    assumed (empty env): a receiver launched with it still gets the post on its next turn, it just
    isn't woken for it."""
    reason = trust.content_hold(msg, sessions.recorded_mode(target_sid), inbound, {})
    if reason is None:
        return None
    return f"held ({_HELD_WORDS.get(reason, reason)}; only the human sees it)"


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def recent_wakes(room, sender_sid, target_sid, now, minutes) -> int:
    since = now - minutes * 60
    return sum(1 for ev in store.read_events(room)
               if ev.get("type") == "wake" and ev.get("decision") == "WAKE"
               and ev.get("from_sid") == sender_sid and ev.get("to_sid") == target_sid
               and _number(ev.get("ts")) and ev["ts"] >= since)


def decide(room, target_sid, sender_sid, urgent, breaker, now=None):
    now = time.time() if now is None else now
    if recent_wakes(room, sender_sid, target_sid, now, breaker["minutes"]) >= breaker["max"]:
        return "WAIT", f"breaker ({breaker['max']} wakes in {breaker['minutes']}m)"
    if not rooms.is_running(target_sid):  # nothing to wake, however warm or urgent
        return "WAIT", "gone (session not running; it will see this when resumed)"
    if urgent:
        return "WAKE", "urgent"
    state, age, _ = sessions.warmth(target_sid, now)
    if state == "warm":
        return "WAKE", "warm"
    last = "never active" if age is None else f"last active {int(age // 60)}m ago"
    return "WAIT", f"cold ({last}; --urgent to force)"


def doorbell_line(name, msg) -> str:
    # escape_text doubles every backslash, so no field can end in a lone "\" before the closing
    # quote; replacing '"' keeps each field inside message="...".
    gist = render.gist(msg.get("text", ""), DOORBELL_GIST_CHARS).replace('"', "'")
    sender = render.escape_text(msg.get("from", "?")).replace('"', "'")
    msg_id = render.escape_text(msg.get("id", "?")).replace('"', "'")
    return f'WAKE {name}: SendMessage(to="{name}", message="{msg_id} {sender}: {gist}")'
