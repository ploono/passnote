# Takeover only when the old member's session is gone

Status: ready-for-agent
Type: task

## What

Decided in the domain-modeling session (2026-09-27), with the terms recorded in `CONTEXT.md` under **Takeover** and **Running / Gone**. A newcomer may take over an existing member's name, along with its cursor, only when that member's session is **gone**. Being **cold** is not enough.

The approved plan (`docs/superpowers/plans/2026-09-27-passnote-phase-a.md`, Task 6) implements takeover with `rooms.is_live()`. That function checks prompt-cache warmth, which is the wrong concept.

**Failure scenario:** Bob's terminal is open, but he has been quiet for 2 hours, so he is cold. Carol runs `passnote join --as bob`. Today she takes over Bob's membership, and Bob's still-running session silently stops receiving messages.

## Change

**1. Replace `is_live(sid)` in the takeover path** with `is_running(sid)`. A session is running when:
- its `by-pid` record (written at join, and on every SessionStart) names a pid that still exists (`os.kill(pid, 0)`), and
- `pid_started_at(pid)` matches the recorded `pid_started_at`, which guards against pid reuse.

If there is no by-pid record, fall back to Claude Code's session registry: `~/.claude/sessions/<pid>.json` (under `CLAUDE_CONFIG_DIR`) is deleted when a session exits (spike A2). If neither source can tell, treat the session as running; the safe default is "clash is an error".

**2. Keep warm/cold for wake decisions only** (`wake.decide`). No change there.

**3. Tighten wording:**
- `post`'s QUEUED reason should say `cold (last active 72m ago; --urgent to force)`, not `idle 72m`, because *idle* means "not in a turn".
- `who` should show both states, for example `warm`/`cold (last active 72m ago)` and `gone` when applicable.

**4. `gc` (amended 2026-10-03, see Comments):** prune members whose sessions are gone. Age alone must not remove a running session's membership. The one exception: a member whose state can't be determined (no by-pid record and no registry file) and that has been inactive for more than 7 days, so a crash can't block a name forever.

## Tests

- A clash with a running but cold member is an error: pid alive with matching start time, active 10 h ago.
- A clash with a gone member is a takeover: pid dead, or start time mismatched. The newcomer inherits the cursor.
- Missing by-pid record and missing registry file → treated as running → clash error.
- QUEUED reason text uses "last active".

## Comments

**2026-10-03: amends §4 (gc).** The Task 18 end-to-end run found that a closed session lost its rooms
as soon as anyone ran `passnote join`, because gc pruned gone members at once. Gone sessions can be
resumed with the same session id (spec §5), so this was wrong.

Decision: gc treats a **gone** member like an **unknown** one. It prunes a session, with its
memberships and cursors, only when the session is gone or unknown **and** has been inactive for more
than 7 days (`--days`). A running session is never pruned. So a resumed session keeps its rooms.

Two things are unchanged:
- Takeover of a gone member's name is still immediate (`join` and `rename` use `is_running`).
- The orphan sweep still removes members with no session dir, or with a key that isn't a session
  id, at once: such a member can't be resumed.
