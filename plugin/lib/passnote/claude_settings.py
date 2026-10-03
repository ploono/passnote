"""Best-effort reads of Claude Code settings files (spec §9).

Only user, project and local settings files are visible here. Managed settings and
`--settings` values never reach hooks, and the README says so.
"""
from __future__ import annotations

import json
import os

from . import config


def claude_home() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def _files(project_dir):
    files = [os.path.join(claude_home(), "settings.json")]
    if project_dir:
        files.append(os.path.join(project_dir, ".claude", "settings.json"))
        files.append(os.path.join(project_dir, ".claude", "settings.local.json"))
    return files


def _layers(project_dir):
    """Each readable settings file's dict, lowest precedence first (user, project, local)."""
    layers = []
    for path in _files(project_dir):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, RecursionError):
            continue
        if isinstance(data, dict):
            layers.append(data)
    return layers


def merged(project_dir=None) -> dict:
    result = {}
    for layer in _layers(project_dir):
        result.update(layer)
    return result


def value(key, project_dir=None):
    return merged(project_dir).get(key)


def inbound(project_dir=None):
    """The strictest crossSessionInbound across all files: a repo's checked-in settings must not
    loosen the user's own "refuse" or "hold" (settings can only make inbound stricter, spec §9)."""
    found = config.strictest(*(layer.get("crossSessionInbound") for layer in _layers(project_dir)))
    return found if found in ("hold", "refuse") else None
