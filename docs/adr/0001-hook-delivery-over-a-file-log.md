# Hook delivery over a plain file log

Messages are delivered by Claude Code hooks (UserPromptSubmit and PostToolBatch). The hooks read each room's append-only log file and add new lines to the turn the receiving session is already taking. There is no MCP server, no daemon and no database.

We measured that the dominant cost of inter-session messaging is extra model turns, each re-reading a ~60k context (11–16k input-token-equivalents per call). Hook delivery costs about the payload plus a ~20-token header, and needs no turn at all (spike A1, `prototype/bus/log.md` a8).

## Considered Options

- **MCP tools to read the room.** Every read is a tool call, which is itself a full model call (~11k), and the tool schemas sit in every session's base context. This is the opposite of what we want.
- **A resident daemon or socket broker.** More to install and keep running, and it still can't wake an idle session any better than a doorbell. It only pays off for cross-machine rooms, which are out of scope.
- **Per-turn polling, as in instavm/murmur.** One tool call per turn plus an always-loaded ~3k-token instruction block, measured and estimated in the spec's §1.
- **hcom-style hooks with SQLite and 13 hooks.** Similar delivery cost (~44 tokens), but it adds 1.4–2.9k tokens to each joined session's base context and needs a binary.

## Consequences

- Delivery happens only when the receiver takes a turn. Waking an idle session needs a separate doorbell (SendMessage), which is rung sparingly.
- Correctness lives in the file protocol: cursors, room locks and redelivery checks against the session transcript.
