import json
import os
import unittest

from support import HomeCase
from passnote import claude_settings, config, paths


def write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh)


class ConfigTest(HomeCase):
    def setUp(self):
        super().setUp()
        paths.ensure_home()

    def test_defaults(self):
        cfg = config.load()
        self.assertEqual(cfg["render_budget_chars"], 2000)
        self.assertEqual(cfg["clip_chars"], 600)
        self.assertEqual(cfg["text_max_chars"], 4000)
        self.assertEqual(cfg["wake_breaker"], {"max": 3, "minutes": 10})
        self.assertIsNone(cfg["ttl_seconds"])
        self.assertEqual(cfg["inbound"], "auto")

    def test_precedence_env_over_room_over_global(self):
        write(os.path.join(self.home, "config.json"), {"clip_chars": 100, "text_max_chars": 900})
        write(os.path.join(self.home, "rooms", "r1", "config.json"), {"clip_chars": 200})
        self.assertEqual(config.load()["clip_chars"], 100)
        self.assertEqual(config.load("r1")["clip_chars"], 200)
        self.assertEqual(config.load("r1")["text_max_chars"], 900)
        os.environ["PASSNOTE_CLIP_CHARS"] = "300"
        self.assertEqual(config.load("r1")["clip_chars"], 300)

    def test_breaker_merge_and_bad_values_ignored(self):
        write(os.path.join(self.home, "config.json"), {"wake_breaker": {"max": 5}, "clip_chars": "lots"})
        cfg = config.load()
        self.assertEqual(cfg["wake_breaker"], {"max": 5, "minutes": 10})
        self.assertEqual(cfg["clip_chars"], 600)

    def test_inbound_is_stricter_only(self):
        write(os.path.join(self.home, "config.json"), {"inbound": "hold"})
        write(os.path.join(self.home, "rooms", "r1", "config.json"), {"inbound": "accept"})
        self.assertEqual(config.load("r1")["inbound"], "hold")
        os.environ["PASSNOTE_INBOUND"] = "refuse"
        self.assertEqual(config.load("r1")["inbound"], "refuse")
        self.assertEqual(config.strictest("auto", "accept"), "auto")

    def test_claude_settings_merge_and_inbound(self):
        project = os.path.join(self.tmp, "proj")
        write(os.path.join(self.claude_home, "settings.json"), {"crossSessionInbound": "accept", "x": 1})
        self.assertIsNone(claude_settings.inbound(project))
        write(os.path.join(project, ".claude", "settings.local.json"), {"crossSessionInbound": "hold"})
        self.assertEqual(claude_settings.inbound(project), "hold")
        self.assertEqual(claude_settings.value("x", project), 1)
        with open(os.path.join(project, ".claude", "settings.json"), "w") as fh:
            fh.write("{broken")
        self.assertEqual(claude_settings.inbound(project), "hold")

    def test_non_ascii_digits_in_env_are_ignored(self):
        # superscript two, Arabic-Indic three, fullwidth five; and more digits than int() accepts
        for raw in ("\u00b2", "\u0663", "\uff15", "1" * 5000):
            with self.subTest(raw=raw):
                os.environ["PASSNOTE_CLIP_CHARS"] = raw
                self.assertEqual(config.load()["clip_chars"], 600)

    def test_claude_settings_inbound_takes_the_strictest_file(self):
        # A checked-in project settings file must never loosen the user's own choice.
        project = os.path.join(self.tmp, "proj")
        user = os.path.join(self.claude_home, "settings.json")
        shared = os.path.join(project, ".claude", "settings.json")
        local = os.path.join(project, ".claude", "settings.local.json")
        cases = [
            ({user: "refuse", shared: "accept"}, "refuse"),
            ({user: "hold", local: "accept"}, "hold"),
            ({user: "refuse", shared: "hold", local: "auto"}, "refuse"),
            ({user: "hold", shared: "REFUSE"}, "refuse"),
            ({user: "accept", shared: "auto"}, None),
        ]
        for files, expected in cases:
            with self.subTest(files=sorted(files.values())):
                for path in (user, shared, local):
                    if os.path.exists(path):
                        os.unlink(path)
                for path, inbound in files.items():
                    write(path, {"crossSessionInbound": inbound})
                self.assertEqual(claude_settings.inbound(project), expected)

    def test_claude_settings_other_keys_stay_last_wins(self):
        project = os.path.join(self.tmp, "proj")
        write(os.path.join(self.claude_home, "settings.json"), {"promptCacheTtl": "1h"})
        write(os.path.join(project, ".claude", "settings.local.json"), {"promptCacheTtl": "5m"})
        self.assertEqual(claude_settings.value("promptCacheTtl", project), "5m")

    def test_null_and_bool_values_ignored(self):
        write(os.path.join(self.home, "config.json"), {
            "clip_chars": None,
            "render_budget_chars": True,
            "ttl_seconds": None,
            "wake_breaker": {"max": True, "minutes": None}
        })
        cfg = config.load()
        # clip_chars null and render_budget_chars bool are ignored, defaults kept
        self.assertEqual(cfg["clip_chars"], 600)
        self.assertEqual(cfg["render_budget_chars"], 2000)
        # ttl_seconds null is accepted
        self.assertIsNone(cfg["ttl_seconds"])
        # wake_breaker bool and null are ignored, defaults kept
        self.assertEqual(cfg["wake_breaker"], {"max": 3, "minutes": 10})

    def test_strictest_normalizes_case_and_ignores_non_strings(self):
        # Uppercase normalization
        self.assertEqual(config.strictest("HOLD"), "hold")
        self.assertEqual(config.strictest("REFUSE"), "refuse")
        self.assertEqual(config.strictest("AUTO"), "auto")
        # Accept has same strictness as auto, so auto wins
        self.assertEqual(config.strictest("Accept"), "auto")
        # Mixed case with stricter values
        self.assertEqual(config.strictest("Hold", "REFUSE"), "refuse")
        self.assertEqual(config.strictest("HOLD", "auto"), "hold")
        # Non-string and unhashable values ignored
        self.assertEqual(config.strictest("hold", None), "hold")
        self.assertEqual(config.strictest("refuse", []), "refuse")
        self.assertEqual(config.strictest({}, "hold", []), "hold")
        # All non-string values fall back to default
        self.assertEqual(config.strictest(None, [], {}), "auto")


if __name__ == "__main__":
    unittest.main()
