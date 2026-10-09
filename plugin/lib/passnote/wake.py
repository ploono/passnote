"""Wake decisions for posts (spec §8): eligibility, holds, cache TTL, warm/cold, breaker, doorbell."""
from __future__ import annotations

import time

from . import paths, render, rooms, sessions, store, trust

ELIGIBLE_KINDS = ("ask", "err")
REPLY_KINDS = ("ans", "nak", "done")
DOORBELL_TEXT = "passnote note waiting"  # no message text (#19): the receiver reads it once, from the hook


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


# The sender's WAIT line for each trust.content_hold reason, plus one for an unknown sender mode.
# PASSNOTE_ALLOW_BYPASS lifts a class mismatch, never an inbound refuse or hold (trust.hold_reason).
_HELD_WORDS = {
    "refuse": "inbound=refuse; only the human sees it",
    "inbound=hold": "inbound=hold; only the human sees it",
    "permission-mode mismatch": "different permission class; only the human sees it unless the receiver allows bypass",
    "receiver mode unknown": "its permission mode is not recorded yet; it is delivered on their next turn if your "
                             "classes match",
    "sender mode unknown": "your permission mode is not recorded yet (join and post in separate turns); no doorbell",
}


def held(msg, target_sid, inbound):
    """The WAIT reason when `msg` may be held from the session target_sid, else None. A post that
    may be held never rings a doorbell: Claude Code would hold it natively, each hold notice costs
    the sender a full turn (A7), and the receiver would not see the message anyway (spec §9).
    Decided with what the sender can know: the post's stamped mode against the
    receiver's recorded mode and the room's or global inbound. Either mode unknown means no
    doorbell (fail closed); for an unknown sender mode (a post in the same command as `join`) this
    is the wake's call only: delivery decides again once the mode is recorded.
    The receiver's own settings and env are invisible here, so PASSNOTE_ALLOW_BYPASS is not
    assumed (empty env): a receiver launched with it still gets the post on its next turn, it just
    isn't woken for it."""
    reason = trust.content_hold(msg, sessions.recorded_mode(target_sid), inbound, {})
    if reason not in ("refuse", "inbound=hold"):
        mode = trust.sender_mode(msg)
        if not (isinstance(mode, str) and mode):
            reason = "sender mode unknown"
    if reason is None:
        return None
    return f"held ({_HELD_WORDS.get(reason, reason)})"


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
    # quote; replacing '"' keeps each field inside message="...". Head fields are clipped like
    # rendered lines (render.FIELD_CLIP), so a forged sender can't make the line unbounded.
    sender = render.escape_text(render._clip_field(msg.get("from", "?"))).replace('"', "'")
    msg_id = render.escape_text(render._clip_field(msg.get("id", "?"))).replace('"', "'")
    return f'WAKE {name}: SendMessage(to="{name}", message="{msg_id} from {sender}: {DOORBELL_TEXT}")'
