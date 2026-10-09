# passnote: listeners and wake rules (Phase D)

**Status:** draft v0.1 (2026-10-09), for maintainer review. Part of #14. Not approved.

Builds on the Phase A design (`2026-09-26-passnote-design.md`, cited as "design §N"), its review (`2026-09-27-passnote-spec-review.md`, F1…F43), and the thread decision in #25 (`post --thread <name>`). The cross-machine spec (`2026-10-09-passnote-cross-machine-rooms.md`) relies on this one to wake idle members on the receiving machine.

**ADR note.** This changes a consequence of ADR-0001: "waking an idle session needs a separate doorbell (SendMessage)". It keeps the decision: listeners are processes that a member's own Claude Code session starts and that end with it, not a resident daemon. If this spec is approved, record a new ADR, "a member's own session can wake it".

## 1. Problem and evidence

Today the only way to wake an idle member is a doorbell. The sender pays for it: one SendMessage tool call in a full session, 11–16k (design §1, F7). The doorbell is printed only for addressed asks, errs, `--wake` posts and replies to the addressee's own ask or prop (design §8).

Two gaps from use:
- **"Wake me when X posts."** Field note on #14: a coordinator bridged two sessions on another machine through comments on a GitHub issue. It ran a hand-rolled poller (every 2 minutes, filtered to the other author) and had to re-arm it after every comment, dozens of times in one night. Each re-arm was a tool call in a full session. What the coordinator wanted was a standing rule that passnote owns.
- **Phase D (design §3, §13).** asyncRewake, the plugin monitor, a headless Stop listener, a direct-socket doorbell, and the deferred `(mode?)` cross-check (Phase A plan deviation 3). Each depends on undocumented Claude Code behaviour, so each is gated on a spike.

## 2. Goals and non-goals

**Goals.**
- A member can say once "wake me when member X (or thread T, or a reply to my ask) posts", and it holds until it expires or is removed. No re-arming.
- A rule is separate from what carries it out. One **wake rule** is evaluated by every **wake path**: the sender's doorbell today, and listeners once their spikes pass.
- A listener wakes an idle member without the sender paying for a doorbell call.
- No path wakes anyone for a message that the receiver would hold.
- Delivery stays in the hook (design §7). Wake paths only start turns; the turn's normal hooks deliver.
- Hook fires stay near-zero cost when nothing is new, and a waiting listener costs 0 tokens.

**Non-goals.**
- Delivering message text through a wake path. A notice says *that* something arrived, never *what*. The one exception is the headless Stop listener, which delivers through the same render path as the hook (§4.6).
- Waking a session across machines directly. A remote line wakes a member only through a listener on that member's own machine (cross-machine spec §5).
- Shipping every Phase D mechanism. The spikes pick one primary listener and one headless listener. The rest are dropped or deferred (§9).

## 3. Proposed vocabulary (for CONTEXT.md once approved)

- **Wake rule**: a member's standing request to be woken when a message matching it is posted in a room it has joined. A rule only ever matches messages that would be delivered to that member.
  _Avoid_: watch (taken by `passnote watch`, the human's live view), subscription (#25 uses it for thread delivery filters), trigger, alert
- **Wake path**: how a wake is carried out: a sender's doorbell, or a listener.
- **Listener**: a process started by a member's own Claude Code session that waits while the session is idle, and wakes it when a message it may be woken for arrives.
  _Avoid_: daemon, watcher, poller
- **Wake notice**: the fixed, content-free line a listener gives the model when it wakes it.

## 4. Design

### 4.1 Wake rules: `passnote wake-on`

```
passnote wake-on [--room r] [--from a,b] [--thread T] [--re <id>] [--kind ans,done,…]
                 [--even-cold] [--once] [--for 12h]
passnote wake-on --list [--room r]
passnote wake-on --off <rule-id>|all [--room r]
```

- At least one of `--from`, `--thread` or `--re` is required, so a rule can't mean "wake me for everything".
- `--from` takes member names; a name that isn't a member is an error. Remote members (cross-machine spec) are allowed.
- `--thread` matches the #25 `thread` field. The rule is refused when the member's thread subscription excludes T, because a rule never matches a message the member wouldn't get.
- `--re <id>` matches any reply (`ans`, `nak`, `done`, `err`) whose `re` is that id. That covers replies to a broadcast ask, which design §8 can't wake for today.
- Rules persist until `--for` expires (default 12 h, maximum 7 days), until `--off`, or until the member leaves the room. `--once` removes a rule after its first wake.
- `--even-cold` lets the rule wake a cold member (Q2). Without it, a cold match waits, as in design §8.
- Output: `ok rule r3 (room r; wakes on: from worker-3; until 21:40; warm only)`. Exit codes follow design §8, plus 3 when the member isn't joined.

**Storage:** `rooms/<room>/wake-rules.json`, `{sid: [{id, from, thread, re, kinds, even_cold, once, expires_at, created_at}]}`. It is rewritten under the room lock, so `post` reads one small file per room. `rooms.carry_over` moves a member's rules across `/clear`. `leave`, `gc` and takeover drop them.

**Matching.** A message `m` matches rule `R` of member `M` only if all of these hold:
1. `trust.visibility(m, M)` is `deliver`: never `skip` (own line, `status`, not addressed to M) and never `hold`;
2. every field the rule sets matches (`from`, `thread`, `re`, `kinds`);
3. the rule hasn't expired.

`wake.is_eligible` becomes "design §8 eligibility **or** a rule match". Everything after that is the existing pipeline, unchanged: `wake.held()` (fail closed on unknown modes), the gone check, the breaker, and warm/cold. A rule match with `--even-cold` is treated like `--urgent`.

`who` lists each member's rules as one line, `wakes on: from worker-3 (12h)`, and `watch` marks wakes caused by a rule.

### 4.2 Files and hook entries

```
rooms/<room>/wake-rules.json              {sid: [rule]}; rewritten under the room lock
sessions/<sid>/listen                     marker: listen on (the Stop guard's only test)
sessions/<sid>/listener.lock              one listener per session (LOCK_NB)
sessions/<sid>/listener.pid               {pid, claude_pid, started_at, path}
sessions/<sid>/listen-cursors/<room>.json the listener's own {ino, off, seq}; never the delivery cursor
sessions/<sid>/ring.jsonl                 ring requests from post, drained by the sender's next hook (L4)
```

Added to `hooks/hooks.json`:
```json
"Stop": [{"hooks": [
  {"type": "command", "command": "/bin/sh", "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/guard.sh", "Listen"], "asyncRewake": true, "timeout": "<D1 ceiling>"},
  {"type": "command", "command": "/bin/sh", "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/guard.sh", "StopListen"], "timeout": "<D3 ceiling>"}
]}]
```
The second entry is Phase L3 only. `guard.sh` needs `Listen` and `StopListen` case arms: today it ends in `*) exit 0`, so an unknown event name silently does nothing.

The two guards are **mutually exclusive**: `Listen` exits when `PASSNOTE_LISTEN=headless`, and `StopListen` runs only when it is set. Otherwise both would fire on one `Stop`, the async `Listen` could take the per-session listener lock first, and `StopListen` would exit without the synchronous delivery of §4.6.

### 4.3 Wake paths

| Path | Who pays | Sessions | Wakes idle? | Gate |
|---|---|---|---|---|
| **Sender doorbell** (today) | sender: one tool call, 11–16k | all with an inbox (A7) | yes | none: Phase L1 |
| **asyncRewake listener** | nobody until a wake | prompting sessions, and non-prompting ones launched with `PASSNOTE_LISTEN=1` (§6.2); never `PASSNOTE_LISTEN=headless` (D1) | to be measured | D1, D2 |
| **Plugin monitor** | nobody until a wake | interactive only; not on Bedrock, Vertex or Foundry | to be measured | D4 |
| **Headless Stop listener** | nobody until a wake | `claude -p` workers launched for it | keeps the worker's turn open | D3 |
| **Direct-socket doorbell** | nobody: rung from the sender's hook | all with an inbox | yes | D5 |

The sender's `post` decides one thing per eligible addressee, in this order:
1. held → `WAIT <name> held (…)` (unchanged);
2. the addressee has a live listener → `LISTEN <name>: its listener wakes it` (no doorbell line, so the sender pays nothing);
3. a direct-socket doorbell is enabled and the version guard passes → `RANG <name>` (rung by the sender's next hook, §4.7);
4. otherwise the existing `WAKE …SendMessage(…)` or `WAIT … cold`.

A **live listener** means `sessions/<sid>/listener.pid` names a running process whose recorded parent `CLAUDE_PID` is alive. If the listener dies after the check, the message waits for the next turn: it is late, never lost.

### 4.4 The listener core: `passnote listen`

One implementation serves every listener path. Only the way it reports a wake differs.

```
passnote listen on|off            per session; writes or removes sessions/<sid>/listen
passnote listen status            armed?, path, pid, last wake, wakes this hour
passnote listen --run <path>      internal: the process a hook or monitor starts
```

**Loop**, in Python, which runs only for sessions that opted in:
1. Take `sessions/<sid>/listener.lock` with `LOCK_NB` and write `listener.pid` (`{pid, claude_pid, started_at}`). A busy lock means another listener is already running for this session: exit 0.
2. Every `listen_poll_seconds` (default 2), `stat` the log of each joined room. Read new lines only when a size changed. The listener has its **own cursor** (`sessions/<sid>/listen-cursors/<room>.json`) and never moves the delivery cursor.
   In a remote room (cross-machine spec), the same poll also runs that spec's pull-due check, so lines from other machines reach the listener while every session here is idle.
3. For each new line, apply §4.1 eligibility and the full wake pipeline (held, breaker, warm/cold) for this member.
4. On the first wake-worthy line, wait `listen_coalesce_seconds` (default 2) for more, then wake once, by the path's method (§4.5–§4.6).
5. Exit 0 with no wake when:
   - `sessions/<sid>/active` changes, because the session took a turn and the hooks deliver;
   - the `CLAUDE_PID` process is gone;
   - `listen off` runs, or the member leaves every room;
   - the path's time limit is reached.

**Wake notice** (asyncRewake and monitor), a fixed template with escaped fields:
`passnote: 2 new for you in room-x (ask b112 from session-b); they arrive with your next tool call or prompt.`
It carries ids, kinds and names, never text. The skill tells the model: on a wake notice, carry on. If there is no tool call to make, run `passnote read --id <id>`, which applies the same holds.

**Budgets:** `listen_max_wakes_per_hour` (default 6) per member, on top of the per-sender breaker. A notice that would exceed the budget is dropped and logged as a `listen-capped` event; the message just waits.

### 4.5 asyncRewake listener (prompting sessions, or non-prompting with `PASSNOTE_LISTEN=1`; never headless; Phase L2)

- A plugin hook entry on `Stop` (the turn ends, so the session goes idle) with `asyncRewake: true` and the longest timeout D1 shows is accepted. Its sh guard exits unless `sessions/$sid/listen` exists and the session's class allows it (§6.2). Then it runs `passnote listen --run rewake`.
- Wake: print the notice and exit 2, which D1 must show starts a turn in an idle session.
- If D1 shows that the rewake turn fires `UserPromptSubmit`, the normal hook delivers in that turn. If it doesn't, delivery comes with the turn's first `PostToolBatch`, or through the skill's `read --id` rule.
- Coverage is limited to the hook's timeout after each turn. A session idle for longer has no listener until its next turn. `listen status` shows `expired`, and `post` falls back to the doorbell. If D1's ceiling is under an hour, the plugin monitor (§4.3) becomes the interactive path instead (Q1).

### 4.6 Headless Stop listener (`claude -p` workers; Phase L3)

For workers a coordinator or a human launches to stay resident, such as a reviewer that handles asks for an afternoon.
- Opt-in only at launch: `PASSNOTE_LISTEN=headless`, plus `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=0`, set by whoever launches the worker. There is no config-file opt-in.
- A synchronous `Stop` hook. Its guard exits unless `PASSNOTE_LISTEN=headless` is set and `stop_hook_active` handling allows it (D3).
- On Stop, under the session `.lock`:
  1. run the normal delivery (design §7 steps 4–8: filters, holds, budget, `last_emit`, cursor advance);
  2. if anything was rendered, return `{"decision":"block", …}` carrying it (field chosen by D3), so the worker continues with the messages in context;
  3. otherwise poll as in §4.4 until a wake-worthy line arrives (then go to step 1) or `PASSNOTE_LISTEN_SECONDS` (default 30 min, bounded by D3's timeout) elapses. Then allow the stop, so the worker exits and `who` shows it gone.
- The systemMessage echo (F26) is emitted as for any delivery, so a human watching the worker's output sees every delivery.
- Delivery here goes through the same render function and the same rendering invariant tests as the hook. It is not a second renderer.
- One listener per session, guaranteed by the §4.4 lock.

### 4.7 Direct-socket doorbell (Phase L4)

- Writing to `CLAUDE_CODE_MESSAGING_SOCKET` directly. Sandboxed Bash can't reach it (A5), but hooks run unsandboxed. So `post` writes a ring request to `sessions/<sender>/ring.jsonl`, and the sender's own next `PostToolBatch` (the one that ends the `post` call) rings. The ring carries the same text-free doorbell that `post` prints (#19, merged in #32): `<id> from <sender>: passnote note waiting`. It carries no message text; the message itself arrives through the hook.
- This removes the sender's 11–16k doorbell call for every warm wake, and lets the cross-machine sync wake a local member when no listener is armed.
- A **version guard** allows it only on Claude Code versions listed after D5 passed on them. Any other version, and any socket error, falls back to the printed `WAKE` line. A ring is never retried blindly.
- **Shipped only if D5 shows** that the receiving Claude Code applies its own inbound holds and `crossSessionInbound` to a socket-written message, and stamps `from-mode` itself rather than trusting the payload. Otherwise it is dropped (Q6).

### 4.8 The `(mode?)` cross-check (Phase L5 or dropped)

Design §9 compares passnote's mode stamp with Claude Code's native `from-mode` on a doorbell. That needs a hook to see the inbound doorbell and its `from-mode`, which D6 measures.
- If a hook can see it: on a mismatch, the message the doorbell names is **held** for this receiver (not just flagged), and the human gets a systemMessage `passnote[r]: b112 held (mode?)`.
- If no hook can see it: drop the item and amend design §9.

### 4.9 Settings

| Key | Where | Default |
|---|---|---|
| `listen_poll_seconds` | room or global config | 2 (minimum 1) |
| `listen_coalesce_seconds` | room or global config | 2 |
| `listen_max_wakes_per_hour` | config, which may only lower it | 6 |
| `PASSNOTE_LISTEN` | launch env only | unset; `1` arms listeners in non-prompting sessions, `headless` enables §4.6 |
| `PASSNOTE_LISTEN_SECONDS` | launch env | 1800 |
| `doorbell` | global config | `print`; `socket` after L4, which the version guard can still overrule |

## 5. Cost per hook fire

| Situation | Extra cost |
|---|---|
| Any session with the plugin, joined or not, `listen` off | delivery hooks unchanged. Hook entries are static, so the new `Stop` entry spawns one `/bin/sh` per turn end; it exits on a missing marker. 0 tokens; Phase C measures the milliseconds |
| `post` | reads one `wake-rules.json` per room. It runs in Bash, not in a hook |
| Listener waiting | 0 tokens. One `stat` per joined room every 2 s; it reads only when a size changed |
| A listener wake | one idle-receiver turn (~15–25k, design §1), the same as a doorbell wake, **minus** the sender's doorbell call (11–16k). A burst coalesced into one wake costs one turn |
| A cold wake (`--even-cold`) | adds a cache write of the receiver's context (2× its size on the 1-hour cache), which is why it is opt-in per rule |
| Headless Stop block | one model call in the worker per block (D3 measures it). The listener exists to replace re-launching workers, which costs more |

Phase C's `bench` gains one counter: wakes by path (doorbell, listener, socket) and the sender calls each one avoided.

## 6. Trust and safety

### 6.1 Wakes never carry or bypass held content

- Every wake path runs the same `trust.visibility` and `wake.held` checks as delivery. A message that is held, or might be held because a mode is unknown, never wakes anyone (fail closed).
- Wake notices are content-free fixed templates. Room names, member names and ids are escaped as in design §7.
- Wake rules match only messages that would be delivered to their owner. A rule can't widen what a member sees.

### 6.2 Who can have a listener

- **Prompting sessions:** `listen on` is enough.
- **Non-prompting sessions** (bypass, auto, unknown): a listener is armed only when the session was launched with `PASSNOTE_LISTEN=1`, or `headless`. This is review F1's rule, "never arm asyncRewake or a Stop listener in one without the launch opt-in". It is a separate variable from `PASSNOTE_ALLOW_BYPASS`, because waking a session that acts without asking is a different risk from letting cross-class text reach it (Q3).
- Subagents never arm listeners. The guard exits on `agent_id`, as in design §5.

### 6.3 Model-writable state

`wake-rules.json` and the `listen` marker are files the model can write. The worst case is more wakes for messages that would be delivered anyway: a cost problem, bounded by the breaker and `listen_max_wakes_per_hour`, which config can only lower. Arming in a non-prompting session needs launch env that the model can't set for its own session.

### 6.4 Direct socket and remote lines

- The socket path ships only if native holds apply to it (§4.7).
- Remote lines (cross-machine spec §6.3) wake through the same pipeline, so a held remote line wakes no one.

## 7. Failure modes

| Failure | Behaviour |
|---|---|
| Listener outlives its session | It checks `CLAUDE_PID` every poll and exits when it is gone. `gc` removes stale pidfiles |
| Two listeners for one session | The lock admits one; the other exits 0 |
| Wake storm (busy room, broad rule) | Coalescing, then the per-sender breaker, then the per-member hourly cap. Excess messages wait for the next turn |
| Rewake arrives while the session is busy | Whatever D1 shows. If it is dropped, nothing is lost, because delivery doesn't depend on the wake |
| The listener's cursor and the delivery cursor diverge | Harmless: the listener only decides when to wake. The delivery cursor alone decides what is delivered |
| Listener died between `post`'s check and the message | `post` printed `LISTEN`, so no doorbell was rung. The message waits for the next turn. `who` shows a dead listener, and the next `post` falls back to `WAKE` |
| `/clear`, compaction, resume | Rules carry with membership (`carry_over`). The listener exits on activity, and the next `Stop` re-arms it |
| A Claude Code update changes rewake or socket behaviour | The version guard and `doctor` check. An unknown version falls back to doorbells |
| A rule names a member that leaves | The rule stays until it expires. `who` marks it `(from: gone)` |

## 8. Spikes (run before each phase's plan)

On macOS and Linux, on the minimum Claude Code version and the newest release. Raw evidence goes to `prototype/spikes/`, with local paths scrubbed.

| id | What it must measure | Gates |
|---|---|---|
| D1 | **asyncRewake.** The largest `timeout` accepted on a plugin `Stop` entry: try 600, 3,600 and 86,400 s, and record whether each is accepted, clamped or rejected. Whether exit 2 in an idle session starts a turn; whether that turn fires `UserPromptSubmit` (and with what `prompt`) and then `PostToolBatch`. What reaches the model (stdout or stderr), what the human sees, and the token cost of a rewake turn when warm and when cold. What exit 2 does mid-turn: dropped, queued or injected. Whether the hook is killed by a new user prompt, `/clear`, or session exit. Behaviour under `-p` | L2 |
| D2 | **Background lifetime** (shared with cross-machine R1/R2). Whether async hooks overlap; whether they are killed at session exit; CPU and wakeups of a 2 s `stat` loop over 8 hours | L2 |
| D3 | **Headless Stop listener.** The largest `Stop` hook timeout. The default block cap and whether `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=0` lifts it. Which of `reason` and `additionalContext` reaches the model, and whether the one-line rendering invariant survives it. Token cost per block. `stop_hook_active` semantics. A reliable signal for "this is `-p`" in hook input or env. Whether a waiting Stop hook stops the worker receiving SendMessage doorbells | L3 |
| D4 | **Plugin monitor.** Manifest form and the `when: on-skill-invoke:passnote` lifecycle (started once? restarted on crash?). Whether a stdout line wakes an idle session or waits for its next turn, and its token cost. Whether `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PID` are in its env. Confirm interactive-only and the Bedrock, Vertex and Foundry gaps | Q1 fallback |
| D5 | **Direct socket.** Framing, how to address another session, and auth. Whether the receiver applies native holds, `crossSessionInbound` and its own `from-mode` stamp to a socket-written message (security-critical: if `from-mode` comes from the payload, the path is dropped). Reachable from a hook process; refused from sandboxed Bash (A5). Stable across three consecutive releases | L4 |
| D6 | **Native `from-mode` in hooks.** Does any hook input (for example `UserPromptSubmit`'s `prompt` for an inbound SendMessage) expose `from-mode` and the doorbell text? | L5 or drop |
| D7 | **Cost.** One idle, warm receiver handling one ask. Compare (a) a printed doorbell, sender plus receiver, (b) an asyncRewake wake, and (c) a socket ring, with the Phase C method, at least 5 runs each | Q8 |

## 9. Open questions (recommendations marked)

1. **Which listener first?** *Recommended:* asyncRewake on `Stop`, if D1 shows a timeout of at least 1 hour and a rewake that starts a turn. It covers prompting sessions, interactive or `-p`, and non-prompting ones launched with `PASSNOTE_LISTEN=1`, on every provider. Workers launched with `PASSNOTE_LISTEN=headless` use the Stop listener instead. The plugin monitor is the fallback for interactive sessions only if D1 fails. Don't ship both.
2. **Do wake rules wake cold members?** *Recommended:* no by default; `--even-cold` per rule. A cold wake re-writes the whole context cache, and the rule's owner is the one who should choose that cost. The field coordinator would set it on its overnight rule.
3. **Opt-in for listeners in non-prompting sessions.** *Recommended:* a separate launch variable, `PASSNOTE_LISTEN=1`, not `PASSNOTE_ALLOW_BYPASS` and never config (review F1).
4. **Names.** *Recommended:* the command `wake-on`, and the glossary terms "wake rule", "wake path", "listener" and "wake notice". `watch` is taken by the human view, and "subscribe" by #25.
5. **Rule lifetime.** *Recommended:* persistent, default `--for 12h`, maximum 7 days, `--once` optional. Re-arming was the field pain, so one-shot shouldn't be the default.
6. **Direct-socket doorbell.** *Recommended:* ship it only if D5 shows native holds and Claude Code's own `from-mode` stamp apply to socket-written messages; otherwise drop it. It saves the most (one sender call per wake), but it is also the easiest way to bypass a guard.
7. **`(mode?)`.** *Recommended:* if D6 shows a hook can see `from-mode`, hold on a mismatch, which is stricter than design §9's flag. Otherwise drop it and amend design §9.
8. **Listen on by default?** *Recommended:* off in L2 and L3. Revisit after D7 and a week of `bench` data. If listener wakes cost no more than doorbell wakes, turn it on by default for prompting sessions.

## 10. Implementation outline

**Phase L0: spikes D1–D7.** Each phase's plan waits only for its own spikes.

**Phase L1: wake rules through the sender's doorbell. No spikes needed.**
- `wake-on` with storage, matching and expiry; eligibility = design §8 or a rule; carry over on `/clear`; the `who` and `watch` lines. `--thread` lands with or after #25.
- *Acceptance:*
  - The field-note case on one machine: the coordinator runs `wake-on --from worker-3 --even-cold` once. Five posts by worker-3, broadcasts included and spaced more than the breaker window apart, each print a `WAKE coordinator` line, with no re-arming.
  - Five such posts within 10 minutes print three `WAKE` lines, then `WAIT … breaker`.
  - A held match prints `WAIT … held` and never `WAKE`.
  - `--once`, `--for` and `--off` work.
  - A rule can't be created for a thread the member doesn't subscribe to.

**Phase L2: the asyncRewake listener** (gated on D1 and D2; the monitor instead if D1 fails).
- `listen on|off|status`, the `Stop` async entry and its guard, the listener loop with its own cursor, coalescing, the wake notice, `post`'s `LISTEN` line, and the non-prompting opt-in.
- *Acceptance:*
  - An idle, warm, prompting member with `listen on` is woken within 5 s of an eligible post, and no SendMessage is used.
  - Its transcript shows no model calls while waiting.
  - A held message wakes no one.
  - The listener exits when the session takes a turn or its process exits.
  - A non-prompting session without `PASSNOTE_LISTEN=1` never arms one.

**Phase L3: the headless Stop listener** (gated on D3).
- *Acceptance:*
  - A `-p` worker launched with `PASSNOTE_LISTEN=headless` handles three asks posted over 30 minutes.
  - Each delivery passes the rendering fuzz test and appears in the systemMessage echo.
  - The worker exits after `PASSNOTE_LISTEN_SECONDS` of quiet.
  - A second listener for the same session exits at once.

**Phase L4: the direct-socket doorbell** (gated on D5), and the cross-machine wake (cross-machine spec Phase 3).
- *Acceptance:*
  - On a guarded version, a warm addressee with no listener is rung by the sender's own hook, with no sender tool call.
  - On an unknown version, `post` prints the usual `WAKE` line.
  - A message held by the receiver's native rules is not delivered through the socket.

**Phase L5: `(mode?)`** (gated on D6), or a design §9 amendment that drops it.
