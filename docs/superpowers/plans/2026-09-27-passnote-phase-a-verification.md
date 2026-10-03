# passnote Phase A: end-to-end verification record

Task 18 of `2026-09-27-passnote-phase-a.md`. Placeholders: `<repo>` is the repository checkout,
`<tmp>` a throwaway temp dir, `<cfg>` a throwaway `CLAUDE_CONFIG_DIR`, `<sid>` a session id.
No session ids, local paths or transcript content are recorded here beyond the test codewords.

- Date: 2026-10-03. Claude Code 2.1.288, macOS (Darwin), python3 3.14 (unit suite also on Apple's 3.9.6).
- Model for every headless session: `haiku`. Sessions started in all: 12 (each test run below says how many; the resume test ran twice).

## Automated checks

### 1. Integration test (`tests/integration/test_headless.py`)

Command (from `<repo>`):

```sh
PASSNOTE_INTEGRATION=1 python3 -X dev -W error -m unittest discover -s tests -p 'test_headless.py' -v
```

Every session runs with `--plugin-dir <repo>/plugin --setting-sources "" --strict-mcp-config
--mcp-config '{"mcpServers":{}}' --model haiku`, a temp `PASSNOTE_HOME`, and without the parent
session's `CLAUDE_CODE_*`, `CLAUDE_PID`, `CLAUDE_PROJECT_DIR`, `CLAUDECODE`, `CLAUDE_EFFORT` and
`PASSNOTE_*` variables. Each test was run on its own (`-k <name>`): once, except the resume test,
which was re-run after the fix.

| Test | Sessions | Result |
|---|---|---|
| `test_message_reaches_a_resumed_session_through_the_hook` (the plan's test) | 3 + 3 | **FAIL** at first (a gc bug, below), then **pass** after the fix |
| `test_message_reaches_a_running_session_through_the_hook_and_after_clear` | 2 | pass |
| `test_doorbell_wakes_an_idle_receiver_that_accepts_cross_session_messages` | 2 | pass |
| `test_a_subagent_cannot_post_as_its_joined_parent` | 1 | pass |

Without `PASSNOTE_INTEGRATION=1` all four are skipped (`OK (skipped=4)`).

**Bug found, then fixed: a resumed session lost its membership.**

*First run (failed).*
- Bob joins in one `claude -p` run, and that process exits. Alice then joins in a second run.
- `join` runs `rooms.gc()` at the end, and gc pruned every member whose session was *gone* (its
  recorded pid no longer exists), whatever its age. So Bob was removed from the room as Alice
  joined:
  - `events.jsonl` read `join bob`, `join alice`, `leave bob` (about 10 ms after Alice's join);
  - `members.json` held only Alice, and Bob's session dir was deleted.
- Alice's `post --to bob` failed (`not in it: bob`), and the resumed Bob answered `NONE`.
- `--resume` itself kept Bob's session id, and `errors.log` stayed empty.

This contradicted spec §5 (resume: "Same sid: keep the cursors"). Any session that was closed and
later resumed lost its rooms as soon as anyone joined any room while it was closed.

*Decision (2026-10-03, amends ticket 01 §4).* gc treats a gone member like an unknown one: it
prunes a session (its memberships, cursors and session dir) only when the session is gone or
unknown **and** has been inactive for more than `max_age_days` (7). A running session is never
pruned. Unchanged: taking over a gone member's name is still immediate, and the orphan sweep still
removes at once any member with no session dir or an invalid session id.

*Fix.*
- `rooms.gc` applies the age rule to gone sessions too.
- The orphan sweep now selects only members without a session dir. Before, it also dropped any
  member whose process was gone.
- Unit tests in `tests/test_rooms.py`:
  - a gone session active recently keeps its membership, cursor, session dir and meta rooms;
  - a gone session idle 8 days is pruned;
  - an unknown session active recently is kept;
  - takeover of a gone member's name is still immediate;
  - the sweep leaves a gone member that still has a session dir to the age rule.
- The resume test also asserts that Bob is still a member before the resume.

*Re-run (pass, 3 sessions).*
- `events.jsonl` reads `join bob`, `join alice` and no `leave`.
- The resumed Bob answered `PERIWINKLE`.
- `errors.log` stayed empty.

**Delivery to a running session, and /clear carry-over** (pass). Bob is a long-lived headless
session fed through `--input-format stream-json`. Alice is a `claude -p` run that joins and posts
in one shell command, so her post is stamped `mode: unknown` and Bob's hook resolves her recorded
mode. Bob's next prompt gets the message through UserPromptSubmit and he answers `PERIWINKLE`. Bob
then gets `/clear` (it works through stream-json input), Alice's CLI posts again under her session
id, and Bob's next prompt (new session id) answers `SAFFRON`. `events.jsonl` has one `carry`
event.

**Doorbell** (pass). The receiver is a stream-json session with `--name pn-it-<hex>` and
`--settings '{"crossSessionInbound":"accept"}'`. It joins under that name and goes idle. The
sender (`claude -p`, `--allowedTools Bash,SendMessage`, with a PreToolUse hook that allows
SendMessage only to the receiver) posts `--kind ask --to pn-it-<hex>`. `post` printed `ok a1` and
`WAKE pn-it-<hex>: SendMessage(to="pn-it-<hex>", message="a1 alice: the codeword is MARIGOLD")`.
`events.jsonl` recorded `wake WAKE … (warm)`. The sender made exactly that SendMessage call. The
idle receiver woke and replied with the codeword. Its passnote cursor also moved past `a1` during
that turn, so a delivery hook fired on the doorbell-started turn too.

**Subagent write guard** (pass). A joined `claude -p` session (`--allowedTools Bash,Agent`) starts
a general-purpose subagent that runs `printf … | passnote post`. The subagent's Bash call came
back `is_error: true` with
`PreToolUse:Bash hook error: passnote: a subagent can't post or change membership as its parent session; …`
and nothing reached the room log. So `permissionDecision: "deny"` blocks the call even though
`--allowedTools Bash` allows Bash. In `-p` mode the Agent tool ran the subagent asynchronously.

### 2. Live hook facts (rulings 4, 4a)

- The exec-form `hooks.json` entries (`"command": "/bin/sh"`, `"args": [...]`) fire in a live
  session.
  - UserPromptSubmit: the delivery tests above. Headless, its `systemMessage` shows as a
    stream-json `system`/`informational` event: `UserPromptSubmit says: passnote[it]: 1 from alice (say a1)`.
  - PostToolBatch: the doorbell decision was `warm`, and only a delivery fire touches `active`.
    The only delivery fire between the receiver's join and the post was the PostToolBatch after
    its join Bash call.
  - SessionStart(clear): a `hook_response` event with
    `passnote: you are bob in rooms it; /passnote for the protocol`.
  - SessionEnd(clear) and PreToolUse: the carry-over and the subagent deny.
- Hook process environment, from one scratch-only run (1 session) with a second exec-form debug
  plugin that logged identity facts per fire (never committed):

  | Fire | stdin `session_id` | env `CLAUDE_CODE_SESSION_ID` | env `CLAUDE_PID` |
  |---|---|---|---|
  | SessionStart(startup), UserPromptSubmit, PreToolUse, PostToolBatch | S0 | S0 | P (= the hook's parent, the claude process) |
  | SessionEnd(reason `clear`) | S0 | S0 | P |
  | SessionStart(source `clear`), then UserPromptSubmit | S1 | **S1** (the new id) | P |
  | SessionEnd(reason `other`, stdin closed) | S1 | S1 | P |

  `CLAUDE_PROJECT_DIR` was set in every fire. Both variables the guard keys on are present, and
  after /clear the environment carries the new session id. `CLAUDE_PID` stays the same across /clear.
- `passnote doctor` run with the post-/clear session id: `ok   hook fired in this session` and no
  `warn`/`FAIL` lines.

### 3. Packaging (rulings 5-7)

| Check | Command | Observed |
|---|---|---|
| Marketplace manifest | `claude plugin validate --strict .` | `✔ Validation passed` |
| Plugin manifest | `claude plugin validate --strict ./plugin` | `✔ Validation passed` |
| Inventory | `CLAUDE_CONFIG_DIR=<cfg> claude --plugin-dir ./plugin plugin details passnote` | `Skills (1) passnote`; `Hooks (5) SessionStart, SessionEnd, UserPromptSubmit, PostToolBatch, PreToolUse (harness-only — no model context cost)`; `Always-on: ~37 tok`; per component `~40` always-on, `~830` on invoke |
| Install | `CLAUDE_CONFIG_DIR=<cfg> claude plugin marketplace add <repo>`, then `claude plugin install passnote@passnote` | marketplace `passnote` added; `passnote@passnote` installed (scope: user). No login needed |
| Cache layout | | `<cfg>/plugins/cache/passnote/passnote/0.1.0/{.claude-plugin,bin,hooks,lib,skills}`, which is the `plugins/cache/<marketplace>/passnote/<version>/bin/passnote` the shim expects |
| Exec bits in the cache | `ls -l` | `bin/passnote` and `hooks/guard.sh` are `-rwxr-xr-x` |
| `installed_plugins.json` shape | keys only | `{"version": 2, "plugins": {"passnote@passnote": [{"scope", "installPath", "version", "installedAt", "lastUpdated", "gitCommitSha"}]}}`, where `installPath` = `<cfg>/plugins/cache/passnote/passnote/0.1.0` |
| Shim | `CLAUDE_CONFIG_DIR=<cfg> plugin/bin/passnote shim --path <tmp>/bin/passnote`, then run it | resolves `<cfg>/plugins/cache/passnote/passnote/0.1.0/bin/passnote` (through `installed_plugins.json`); `--version` prints `passnote 0.1.0`. With a config dir that has no install it prints `passnote: plugin not found; install it with /plugin install passnote@<marketplace>` and exits 2 |
| Update (no session) | copy of the marketplace at 0.1.0; install; bump `plugin.json` to 0.1.1; `claude plugin marketplace update passnote`; `claude plugin update passnote@passnote` | `updated from 0.1.0 to 0.1.1 … Restart to apply changes.` The cache holds `0.1.0/` (now with `.orphaned_at`) and `0.1.1/`; `installPath` points at `0.1.1` (no `gitCommitSha`: the copy is not a git checkout), and the shim resolves `0.1.1/bin/passnote` |

Deviations:

- **Always-on cost depends on the config.** On the same Claude Code 2.1.288,
  `claude --plugin-dir ./plugin plugin details passnote` estimates:
  - **~37** tokens always-on (~40 per component, ~830 on invoke) with a fresh, empty
    `CLAUDE_CONFIG_DIR`;
  - **~58** (~60, ~1.2k on invoke) with an established user config, which is what Task 17 measured.

  The on-invoke figure moves by the same ratio, so the estimator probably counts with a
  config-dependent tokenizer or model; this was not confirmed. The README now says "about 40–60"
  and names both measurements.
- **Local caches.** A local-path marketplace copies the directory as it is on disk, untracked
  `__pycache__/` included. A git-sourced install only has tracked files. This is harmless.
- `passnote --version` reads `passnote/__init__.py`. The update check bumped only `plugin.json`,
  so the 0.1.1 copy still printed `0.1.0`. A real release bumps both, and `test_packaging`
  enforces that they match.

### 4. Unit suite

`python3 -m unittest discover -s tests -v`, `python3 -X dev -W error -m unittest discover -s tests`
and `/usr/bin/python3 -m unittest discover -s tests`: 412 tests after the gc fix (408 before it),
`OK (skipped=4)`. The 4 skipped are the integration tests.

## Manual checks (to be run by the user)

Two interactive terminals in `<repo>`, each started with `claude --plugin-dir ./plugin`. The
checklist with the exact commands and expected strings was handed over separately. Record what
you observe here, using the same placeholders, with no session ids or local paths.

| # | Check | Observed | Deviations |
|---|---|---|---|
| M1 | `/rename alice` / `/rename bob`, then `passnote join` with no `--as`: `joined claude-code-communication (claude-code-communication-xxxx) as alice` | Both joined room `claude-code-communication (claude-code-communication-<4hex>)`, rooted at the main checkout, members listed correctly. | Both models called ListAgents and ran `passnote join --as <name>`, so the no-`--as` session-registry naming path was not exercised live (covered by unit tests). |
| M2 | Ask from A to bob: `ok a1`, then `WAKE bob: …` (A sends exactly that SendMessage) or `WAIT bob cold (last active …; --urgent to force)` | `ok a1` then `WAKE bob: SendMessage(...)`; alice sent exactly the printed doorbell; bob (idle) woke and answered 11 s later. Room log: `wake WAKE bob a1 (warm)`. | None |
| M3 | How the terminal shows `systemMessage` in B: `passnote[claude-code-communication]: 1 from alice (ask a1)` (the open spike item) | Renders once, as a dim sub-line under the incoming turn: `UserPromptSubmit says: passnote[claude-code-communication]: 1 from alice (ask a1)`; alice similarly saw `… 1 from bob (ans b2)`. Readable, not repeated. | None |
| M4 | B answers with `passnote post --kind ans --re a1 --to alice` | Bob posted `--kind ans --re a1 --to alice` → id `b2` (ids are member alias + room-wide sequence); it rang alice's doorbell (`wake WAKE alice b2 (warm)`); alice did not acknowledge the answer (skill's no-acks rule). | The checklist's example `b1` was incorrect; actual id was `b2`. |
| M5 | `/clear` in B, A posts again, B still receives it | Room events show `carry <old-sid-prefix> → <new-sid-prefix>`; alice's plain `say` printed only `ok a3` (no doorbell for a say); bob's first prompt after /clear showed `UserPromptSubmit says: passnote[claude-code-communication]: 1 from alice (say a3)`; asked unambiguously, bob quoted `a3 alice→you say: still there?` and identified it as from another session; the SessionStart(clear) identity line `passnote: you are bob in rooms claude-code-communication; /passnote for the protocol` was received. Did you receive any messages from other Claude sessions? Bob received `a3 alice→you say: still there?` from alice's earlier session. | Resume carry-over also verified: both sessions exited and resumed with `claude --plugin-dir ./plugin --resume`; `who` still listed both members, warm, not gone (live check of the 2026-10-03 gc decision). |
| M6 | `passnote doctor` in both: no `FAIL` lines | Both sessions: six `ok` lines (Claude Code 2.1.288, python 3.14, platform Darwin, storage private, storage writable from this shell, hook fired in this session), no warn/FAIL, exit 0. | None |
| M7 | `passnote watch --all` from a plain terminal: messages and wake/join events live | `passnote watch --all` from a plain terminal printed the history (joins, ask in red, ans in green, wake and carry events dimmed) and a new post (`a4 alice→bob say: watch test`) appeared live. | None |
| M8 | `claude plugin update` with a joined session running | Not run; deferred by the user until the first published release. The file side of an update was covered by the automated throwaway-config check. | Deferred |
