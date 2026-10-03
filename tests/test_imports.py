import json
import subprocess
import sys
import unittest

from support import LIB

# Every delivery hook imports these, on every prompt and tool batch.
HOOK_MODULES = ("paths", "store", "cursor", "sessions", "render", "trust", "config", "claude_settings",
                "wake", "fold", "rooms", "transcript", "hook")
# Slow to import and rarely needed by a hook: imported inside the functions that use them (P1).
# uuid is here too: on Linux CPython <= 3.11 it imports platform, which imports subprocess.
LAZY = ("tempfile", "subprocess", "shutil", "hashlib", "uuid", "platform")

PROBE = """
import json, sys
sys.path.insert(0, {lib!r})
import {modules}
print(json.dumps(sorted(m for m in {lazy!r} if m in sys.modules)))
"""


class HookImportTest(unittest.TestCase):
    def test_hook_modules_do_not_import_tempfile_or_subprocess(self):
        code = PROBE.format(lib=LIB, lazy=LAZY, modules=", ".join(f"passnote.{m}" for m in HOOK_MODULES))
        out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [])


if __name__ == "__main__":
    unittest.main()
