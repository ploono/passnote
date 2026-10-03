"""Confirm deliveries from the session transcript (spec §7 guarantee, spike A6).

When another plugin's UserPromptSubmit hook blocks a prompt, our additionalContext is dropped
silently. The next fire checks that the lines it emitted appear in a passnote-headed
hook_additional_context record (the ids, for an emit state written by an older passnote);
anything missing is redelivered once. The transcript format is
internal to Claude Code, so an unreadable transcript returns None and the check is skipped.
"""
from __future__ import annotations

import os

from . import store
from .render import HEADER


def unconfirmed(path, refs):
    """The emitted refs the transcript doesn't show delivered, or None when it can't be read.
    A ref holding the line the hook emitted for it is confirmed only by that exact whole line: ids
    are alias+seq per room, so the same id delivered from another room proves nothing. A ref
    without one (emit state written by an older passnote) is confirmed by its id."""
    lines = headed_lines(path)
    if lines is None:
        return None
    by_id = _ids_in(lines, {ref.get("id") for ref in refs
                            if not isinstance(ref.get("line"), str) and isinstance(ref.get("id"), str)})
    return [ref for ref in refs if not (ref["line"] in lines if isinstance(ref.get("line"), str)
                                        else isinstance(ref.get("id"), str) and ref["id"] in by_id)]


def _ids_in(lines, wanted):
    """The ids in `wanted` that start a line (after an optional [room] prefix)."""
    found = set()
    for line in lines:
        tokens = line.split(" ")
        if tokens and tokens[0].startswith("[") and len(tokens) > 1:
            tokens = tokens[1:]
        if tokens and tokens[0] in wanted:
            found.add(tokens[0])
    return found


def headed_lines(path):
    """{line} of every passnote-headed hook_additional_context record in the transcript's tail,
    or None when the transcript can't be read."""
    if not path:
        return None
    if not os.path.isfile(path) or not os.access(path, os.R_OK):
        return None
    found = set()
    for raw in store.tail_lines(path):
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
        found.update(text.split("\n"))
    return found
