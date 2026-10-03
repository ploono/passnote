"""Settings with precedence env > room > global > default (spec §10)."""
from __future__ import annotations

import os

from . import paths

DEFAULTS = {
    "render_budget_chars": 2000,
    "clip_chars": 600,
    "text_max_chars": 4000,
    "wake_breaker": {"max": 3, "minutes": 10},
    "ttl_seconds": None,
    "inbound": "auto",
}
ENV_INTS = {
    "render_budget_chars": "PASSNOTE_RENDER_BUDGET_CHARS",
    "clip_chars": "PASSNOTE_CLIP_CHARS",
    "text_max_chars": "PASSNOTE_TEXT_MAX_CHARS",
    "ttl_seconds": "PASSNOTE_TTL_SECONDS",
}
INBOUND_STRICTNESS = {"accept": 0, "auto": 0, "hold": 1, "refuse": 2}


def strictest(*values) -> str:
    """Inbound can only get stricter; 'accept' never loosens the default.

    Normalizes string values to lowercase; ignores non-string and unhashable values.
    """
    best = "auto"
    for value in values:
        # Normalize string values to lowercase; ignore non-string values
        if isinstance(value, str):
            normalized = value.lower()
        else:
            continue

        if normalized in INBOUND_STRICTNESS and INBOUND_STRICTNESS[normalized] > INBOUND_STRICTNESS[best]:
            best = normalized
    return best


def _layers(room):
    layers = [paths.read_json(os.path.join(paths.home(), "config.json"), {})]
    if room:
        layers.append(paths.read_json(os.path.join(paths.room_dir(room), "config.json"), {}))
    return [layer for layer in layers if isinstance(layer, dict)]


def load(room: str | None = None) -> dict:
    cfg = {key: (dict(value) if isinstance(value, dict) else value) for key, value in DEFAULTS.items()}
    inbound = []
    for layer in _layers(room):
        for key, value in layer.items():
            if key == "inbound":
                inbound.append(value)
            elif key == "wake_breaker" and isinstance(value, dict):
                for part in ("max", "minutes"):
                    v = value.get(part)
                    if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                        cfg["wake_breaker"][part] = v
            elif key in ENV_INTS and not isinstance(value, bool):
                # Allow None only for ttl_seconds; other int keys must be int > 0
                if key == "ttl_seconds" and value is None:
                    cfg[key] = value
                elif isinstance(value, int) and value > 0:
                    cfg[key] = value
    for key, var in ENV_INTS.items():
        parsed = paths.ascii_int(os.environ.get(var, ""))
        if parsed:
            cfg[key] = parsed
    inbound.append(os.environ.get("PASSNOTE_INBOUND"))
    cfg["inbound"] = strictest(*inbound)
    return cfg
