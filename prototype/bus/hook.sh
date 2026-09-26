#!/bin/bash
# Deliver new bus/log.md lines as hook additionalContext (no wakeup, no tool call).
# Per-session cursor = line count already delivered. Silent (no output) when nothing new.
exec python3 -c '
import json, os, sys
d = json.load(sys.stdin)
sid = d.get("session_id", "x")[:8]
ev = d.get("hook_event_name", "UserPromptSubmit")
bus = os.path.join(os.path.dirname(os.path.abspath(sys.argv[1])))
log, cur = os.path.join(bus, "log.md"), os.path.join(bus, ".cursor-" + sid)
try: lines = open(log).read().splitlines()
except FileNotFoundError: sys.exit(0)
try: n = int(open(cur).read())
except Exception: n = len(lines)  # first run: start at end, no history dump
new = lines[n:]
open(cur, "w").write(str(len(lines)))
if not new: sys.exit(0)
print(json.dumps({"hookSpecificOutput": {"hookEventName": ev,
  "additionalContext": "bus+:\n" + "\n".join(new)}}))
' "$0"
