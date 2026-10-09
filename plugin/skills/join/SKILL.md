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

Tell the user the result above in one line and run nothing. Only if it reads "[shell command execution disabled by policy]", run `passnote join` yourself, adding `--as <name>` if a name was given here: "$ARGUMENTS".
