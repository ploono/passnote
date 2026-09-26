#!/usr/bin/env bash
# Usage: bus/dispatch.sh "<precise task>" [data-file|-]
# Runs a lean headless worker, appends result to bus/log.md as ## w<n>, prints result only.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TASK="${1:?task required}"; SRC="${2:-}"
MODEL="${DISPATCH_MODEL:-haiku}"
LOG="$ROOT/bus/log.md"; LOCK="$ROOT/bus/.log.lock"
if [ "$SRC" = "-" ]; then DATA="$(cat)"; elif [ -n "$SRC" ]; then DATA="$(cat "$SRC")"; else DATA=""; fi

PROMPT="$(printf '<data>\n%s\n</data>\n<task>%s</task>' "$DATA" "$TASK")"
OUT="$(printf '%s' "$PROMPT" | (cd "$ROOT" && MAX_THINKING_TOKENS="${DISPATCH_THINK:-0}" claude -p --output-format json --no-session-persistence \
  --strict-mcp-config --mcp-config worker/empty-mcp.json --setting-sources "" \
  --disable-slash-commands --no-chrome --system-prompt-file worker/sys.txt \
  --model "$MODEL" --tools ""))"

PARSE='import json,sys
d=json.load(sys.stdin);u=d.get("usage",{})
if sys.argv[1]=="u": print("in=%s out=%s $=%.4f"%(u.get("input_tokens",0)+u.get("cache_read_input_tokens",0)+u.get("cache_creation_input_tokens",0),u.get("output_tokens",0),d.get("total_cost_usd",0)))
else:
  r=(d.get("result") or "").strip()
  print("ERR w why="+r.replace("\n"," ")[:200] if d.get("is_error") else r)'
RESULT="$(printf '%s' "$OUT" | python3 -c "$PARSE" r)"
USAGE="$(printf '%s' "$OUT" | python3 -c "$PARSE" u)"

# mkdir lock (atomic on POSIX); stale lock after ~10s is broken
for i in $(seq 1 100); do mkdir "$LOCK" 2>/dev/null && break; sleep 0.1; [ "$i" = 100 ] && rm -rf "$LOCK" && mkdir "$LOCK"; done
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT
N=$(( $(grep -c '^## w[0-9]' "$LOG" 2>/dev/null || true) + 1 ))
RESULT="${RESULT/ w / w$N }"
printf '\n## w%s\ntask: %s\n%s\nusage %s model=%s\n' "$N" "$(printf '%s' "$TASK" | tr '\n' ' ')" "$RESULT" "$USAGE" "$MODEL" >> "$LOG"
printf '%s\n' "$RESULT"
