#!/bin/sh
# passnote hook guard (spec §7 step 1). Lets a hook fire through to Python only when it can matter
# for this session, so unjoined sessions pay a few shell builtins and zero tokens. Always exits 0.
# Order: the file tests below are builtins (no fork); uname and python3 come after them.
# No Python version check here: hook.main returns nothing below 3.9.
event="$1"

# $1 resolved the way passnote's paths.home() resolves a variable: an absolute path as is, "~" and
# "~/..." from $HOME, a relative value ignored (r=""). Another "~" form (~user/...) needs Python's
# expansion, so it is left to Python (r="?").
absolute() {
  case "$1" in
    /*) r="$1" ;;
    \~|\~/*) if [ -n "${HOME+set}" ]; then r="$HOME${1#\~}"; else r="?"; fi ;;
    \~*) r="?" ;;
    *) r="" ;;
  esac
}

absolute "${PASSNOTE_HOME-}"
home="$r"
if [ -z "$home" ]; then
  absolute "${XDG_STATE_HOME-}"
  if [ "$r" = "?" ]; then
    home="?"
  elif [ -n "$r" ]; then
    home="$r/passnote"
  elif [ -n "${HOME+set}" ]; then
    home="$HOME/.local/state/passnote"
  else
    home="?"
  fi
fi
sid="${CLAUDE_CODE_SESSION_ID:-none}"
pid="${CLAUDE_PID:-none}"
case "$pid" in
  *[!0-9]*) pid=none ;;
esac
by_pid="$home/sessions/by-pid/$pid.json"

joined() {
  [ "$home" = "?" ] || [ -e "$home/sessions/$sid/joined" ]
}

# SessionEnd(clear) left the old sid in this process's by-pid record and the carry to this sid
# didn't finish (hook._carry_over_from_clear): a delivery fire finishes it.
carry_pending() {
  [ -e "$by_pid" ] || return 1
  record=""
  { IFS= read -r record; } 2>/dev/null <"$by_pid"
  case "$record" in
    *"\"prev_sid\": \"$sid\""*) return 1 ;;
    *'"prev_sid"'*) return 0 ;;
  esac
  return 1
}

input=""
case "$event" in
  UserPromptSubmit|PostToolBatch)
    joined || carry_pending || exit 0
    ;;
  SessionStart|SessionEnd)
    # A resumed session runs in a new process, and a /clear's new sid has joined nothing yet.
    joined || [ -e "$by_pid" ] || exit 0
    ;;
  PreToolUse)
    # Joined or not: a subagent of an unjoined session must not join as its parent either. Only a
    # subagent's Bash command that mentions passnote can be denied, so "passnote" must come after
    # the "command" key (not just in cwd or transcript_path). The input is one JSON line.
    line=""
    while IFS= read -r line; do
      input="$input$line
"
    done
    input="$input$line"
    case "$input" in
      *'"command"'*passnote*) ;;
      *) exit 0 ;;
    esac
    case "$input" in
      *'"agent_id"'*) ;;
      *) exit 0 ;;
    esac
    ;;
  *)
    exit 0
    ;;
esac

case "$(uname -s 2>/dev/null)" in
  Darwin|Linux) ;;
  *) exit 0 ;;
esac
command -v python3 >/dev/null 2>&1 || exit 0
case "$0" in
  */*) dir="${0%/*}" ;;
  *) dir="." ;;
esac
if [ "$event" = "PreToolUse" ]; then
  printf '%s' "$input" | python3 -I "$dir/../bin/passnote" hook "$event"
  exit 0
fi
exec python3 -I "$dir/../bin/passnote" hook "$event"
