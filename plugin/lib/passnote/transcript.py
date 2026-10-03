"""Confirm deliveries from the session transcript (spec §7 guarantee, spike A6).

When another plugin's UserPromptSubmit hook blocks a prompt, our additionalContext is dropped
silently. The next fire checks that the ids it emitted appear in a passnote-headed
hook_additional_context record; anything missing is redelivered once. The transcript format is
internal to Claude Code, so an unreadable transcript returns None and the check is skipped.
"""
from __future__ import annotations

import os

from . import store
from .render import HEADER


def delivered_ids(path, ids):
    if not path:
        return None
    if not os.path.isfile(path) or not os.access(path, os.R_OK):
        return None
    lines = store.tail_lines(path)
    wanted = set(ids)
    found = set()
    for raw in lines:
        if b"hook_additional_context" not in raw:
            continue
        rec = store.parse(raw)
        attachment = rec.get("attachment") if rec else None
        if not isinstance(attachment, dict) or attachment.get("type") != "hook_additional_context":
            continue
        content = attachment.get("content")
        if isinstance(content, list):
            text = "\n".join(part for part in content if isinstance(part, str))
        else:
            text = str(content or "")
        if HEADER not in text:
            continue
        for line in text.split("\n"):
            tokens = line.split(" ")
            if tokens and tokens[0].startswith("[") and len(tokens) > 1:
                tokens = tokens[1:]
            if tokens and tokens[0] in wanted:
                found.add(tokens[0])
    return found
