---
name: join
description: Join this repo's passnote room
disable-model-invocation: true
argument-hint: "[name]"
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/passnote join *)
---
```!
${CLAUDE_PLUGIN_ROOT}/bin/passnote join --name-stdin <<'PASSNOTE_JOIN_NAME_END'
[$ARGUMENTS]
PASSNOTE_JOIN_NAME_END
```

Tell the user the result above in one line and run nothing. Only if it reads "[shell command execution disabled by policy]", run `passnote join` yourself. If a name was given here ("$ARGUMENTS"), add `--as <name>` only when it is 1-64 of A-Z a-z 0-9 . _ -; otherwise run nothing and tell the user the name is invalid.
