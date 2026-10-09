"""Fold the room log into who/watch state (spec §6 'Unanswered', §10 who). Pure functions."""
from __future__ import annotations

from . import store

_BRIEF_KEYS = ("id", "seq", "from", "sid", "to", "kind", "re", "text")


def _brief(msg):
    return {key: msg.get(key) for key in _BRIEF_KEYS}


def passed(seq, cur) -> bool:
    """Whether a member's cursor `cur` has passed the message with `seq` (the "seen" rule)."""
    s = cur.get("seq") if isinstance(cur, dict) else None
    return isinstance(s, int) and not isinstance(s, bool) and isinstance(seq, int) and s >= seq


def fold(messages, members, cursors, held=None) -> dict:
    """`held` is an optional set of (message id, addressee sid): a prop held back from its addressee
    (a hold event) was not seen by them, whatever their cursor says."""
    held = held or set()
    # Any member can write a raw log line or members.json: skip malformed entries up front.
    messages = [msg for msg in messages if store.valid_message(msg)]
    members = {sid: info for sid, info in members.items()
               if isinstance(info, dict) and isinstance(info.get("name"), str)}
    sid_of = {info["name"]: sid for sid, info in members.items()}

    def name_of(msg):
        """The sender's member name (its sid now, or before a /clear), else the claimed `from`."""
        found = store.member_for_sid(members, msg.get("sid"))
        return found[1]["name"] if found else msg.get("from")

    replied = {}
    for msg in messages:
        if msg.get("re"):
            replied.setdefault(msg["re"], set()).add(name_of(msg))

    unanswered = []
    for msg in messages:
        to = msg.get("to")
        if msg.get("kind") in ("ask", "err") and isinstance(to, list):
            waiting = [name for name in to if name not in replied.get(msg.get("id"), set())]
            if waiting:
                unanswered.append(dict(_brief(msg), waiting_on=waiting))

    released = {msg.get("re") for msg in messages
                if msg.get("kind") == "claim" and msg.get("re") and msg["text"].strip() == "release"}
    claims = [_brief(msg) for msg in messages
              if msg.get("kind") == "claim" and not msg.get("re")
              and msg.get("id") not in released and store.member_for_sid(members, msg.get("sid")) is not None]

    status = {}
    for msg in messages:
        if msg.get("kind") == "status":
            status[name_of(msg)] = msg["text"]

    props = []
    for msg in messages:
        if msg.get("kind") == "prop" and isinstance(msg.get("to"), list):
            seen, unseen = [], []
            for name in msg["to"]:
                addressee = sid_of.get(name)
                done = passed(msg.get("seq", 0), cursors.get(addressee)) and (msg.get("id"), addressee) not in held
                (seen if done else unseen).append(name)
            props.append(dict(_brief(msg), seen=seen, unseen=unseen))
    return {"unanswered": unanswered, "claims": claims, "status": status, "props": props}
