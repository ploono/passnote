# passnote: rooms across machines

**Status:** draft v0.1 (2026-10-09), for maintainer review. Part of #24. Not approved.

Builds on the Phase A design (`2026-09-26-passnote-design.md`, cited as "design §N") and its review (`2026-09-27-passnote-spec-review.md`, finding ids F1…F43). Waking idle members is the job of the listeners spec (`2026-10-09-passnote-listeners.md`); this spec depends on it only for §9's "idle receivers" phase.

**ADR note.** This reopens a non-goal (design §2: "cross-machine rooms") and the scope note in ADR-0001 ("a resident daemon … only pays off for cross-machine rooms, which are out of scope"). It keeps ADR-0001's decision itself: hook delivery from a plain local file log, and no resident daemon. If this spec is approved, record a new ADR, "a room backend syncs a remote log into the local one".

## 1. Problem and evidence

A room is a local directory, so a session on another machine can't be a member.

Field evidence (#24): a coordinator with about 15 worker sessions on one machine also directed two sessions on a second machine. passnote couldn't reach those two, so the team used one GitHub issue per task as the shared log, plus a hand-rolled poller that read new comments every 2 minutes and had to be re-armed after every comment. That was the busiest coordination path of the period. The reporter ranked rooms across machines as the change that would most have made passnote their default.

What the workaround cost, in design §1 units:
- each poll that found something was a tool call in a full session (11–16k);
- each re-arm was another call;
- each remote reply was read by hand, outside hook delivery.

passnote can do the same job with the hook doing the reading.

## 2. Goals and non-goals

**Goals.**
- Members on two or more machines share one room, and exchange asks and replies with hook delivery on every machine.
- The local log stays the only thing the hook reads. A **room backend** copies lines between the local log and a shared **remote log**.
- Remote rooms are opt-in per room, by a human, and `doctor` says which backend each room uses (#24 acceptance).
- Holds and the secret guard apply across machines, and they fail closed.
- A hook fire costs nothing extra when nothing is new: no network, no tokens, and at most one `stat` per remote room.

**Non-goals.**
- Waking a member on another machine. A post never rings a doorbell across machines (§5). Remote wakes come from a listener on the receiving machine (listeners spec).
- Real-time latency. Seconds to a minute is fine; the field baseline was 2 minutes plus re-arming.
- A hosted service. passnote ships no server in v1 (Q1).
- Sharing a `PASSNOTE_HOME` over a network filesystem. That stays unsupported (design §4, F39).
- Moving a member between machines. Takeover stays within one machine (Q7).
- Encryption at rest on the backend. The backend's own access control is the boundary (§6).

## 3. Proposed vocabulary (for CONTEXT.md once approved)

- **Room backend**: where a room's log is shared beyond this machine. `local` (the default, as today) or a remote backend such as `github-issue`.
- **Remote log**: the backend's copy of the room's log. Every machine syncs to and from it.
- **Sync**: copying new local lines to the remote log (push) and new remote lines into the local log (pull).
- **Machine**: one `PASSNOTE_HOME`. It has a random machine id and a human-chosen label.
- **Remote member**: a member whose session runs on another machine. It is neither running nor gone from this machine's point of view; it is *remote*.
- **Origin**: the machine a message was first posted on.

## 4. Design

### 4.1 Shape

```
machine A                                     machine B
 session ─post─► rooms/r/log.jsonl ──push──►  remote log  ──pull──► rooms/r/log.jsonl ─hook─► session
 session ◄─hook─ rooms/r/log.jsonl ◄──pull──  (backend)   ◄──push── rooms/r/log.jsonl ◄─post─ session
```

- `post` never touches the network. It appends to the local log, as today, and touches `remote/push-wanted`.
- Sync runs outside the delivery hook, in an **async hook entry** (§4.4). It pushes local-origin lines and pulls remote lines into the local log through the normal append path.
- The delivery hook is unchanged: it reads the local log from its cursor. A pulled line is delivered on the receiver's next turn, like any other line.

### 4.2 Local layout additions

```
machine.json                          {machine_id (random 128-bit hex), label}
rooms/<room>/remote.json              {backend, target, room_key, attached_at, pull_seconds}
rooms/<room>/remote/state.json        {etag, since, seen_rids (bounded), last_pull, last_push, last_error, suspended}
rooms/<room>/remote/push.json         push cursor {ino, off, seq} over log.jsonl
rooms/<room>/remote/push-wanted       touched by post; its mtime is newer than last_push when a push is due
rooms/<room>/remote/pulled            mtime = start of the last pull; the async guard's only stat
rooms/<room>/remote/.sync.lock        one sync per room per machine (LOCK_NB; busy means skip)
sessions/<sid>/remote                 marker: this session is joined to at least one remote room
```

### 4.3 Message and member fields

**Message fields added** (all optional, so `store.valid_message` accepts old lines):

| Field | On | Meaning |
|---|---|---|
| `origin` | every line in a remote room | machine id of the origin, short form (first 12 hex) |
| `mkey` | every line in a remote room | the sender's **member key**: 16 random hex chars minted at `join`, stored in the local `members.json` entry and moved by `carry_over` like `alias`, so it survives `/clear`. Never derived from the sid. Replaces `sid` on the wire |
| `rid` | pulled lines | the backend's record id (a comment id for `github-issue`) |
| `author` | pulled lines | the backend-authenticated principal (a GitHub login) |
| `verified` | pulled lines | `true` only when `author` is the principal bound to `origin` (§6.3) |

**What changes for pulled lines** (code traps found in `store.py`, `hook.py`, `rooms.py`, `wake.py`):
- **seq is local.** `append_message` assigns `seq = last_seq + 1` under the local room lock, and `_read_room` skips any line whose seq is below the cursor's as forged. A pulled line is therefore appended through the local lock with a **fresh local seq**. Its origin seq is never written into the local `seq` field.
- **id is global.** `re`, `read --id`, unanswered tracking and `last_emit` all key on `id`. A pulled line keeps its **origin id verbatim**. That works only if aliases are unique across machines, which member records guarantee (§4.5). `append_message` gains a path that keeps a given id instead of minting `alias+seq`.
- **sid is never shared.** On the wire `sid` is replaced by `mkey`; a pulled line's local `sid` field is set to `remote:<mkey>`. Code that treats `sid` as a session UUID (`sessions.recorded_mode`, `paths.check_sid`) must treat the `remote:` prefix as "no local session" and never build a path from it.
- **Remote members are not gone.** `rooms.is_running` returns False for a sid with no local session dir, so today a remote member would be taken over by a local newcomer, pruned by `gc` and `_sweep_orphan_members`, and reported "gone" by `wake.decide`. Remote members are stored in `members.json` under `remote:<mkey>` with `remote: true, machine, author`, and every one of those paths treats them as a third state: not takeover-able, not gc'd while the room is attached, and `WAIT … remote` in `post`.

**Never leaves the machine:** `sid`, `root`, `transcript_path`, `permission_mode` history, cursors, `events.jsonl`, holds, `errors.log`. The wire carries only the message fields of design §6 (minus `sid`), plus `origin` and `mkey`, plus the control records below.

### 4.4 Sync

**Trigger.** A second hook entry on `UserPromptSubmit` and `PostToolBatch` with `"async": true`, so it never delays the turn (spike R1). Its guard is sh-only, like `guard.sh`:
1. exit unless `sessions/$sid/remote` exists (sessions in local rooms only stop here: one file test);
2. for each remote room, `stat` `remote/pulled` and `remote/push-wanted`; exit unless a pull is due (`now - mtime(pulled) >= pull_seconds`) or a push is due (`push-wanted` newer than the last push);
3. run `passnote sync --room <r>` under `.sync.lock` with `LOCK_NB`. A busy lock means another session on this machine is already syncing: exit.

Added to `hooks/hooks.json` (one entry per event, alongside the existing synchronous ones):
```json
{"type": "command", "command": "/bin/sh", "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/guard.sh", "Sync"], "async": true, "timeout": 30}
```
`guard.sh` needs a new `Sync` case arm: today it ends in `*) exit 0`, so an unknown event name silently does nothing.

So a machine pulls each remote room at most once per `pull_seconds` however many sessions it runs, and a post is pushed by the sender's own next `PostToolBatch` (the one that ends the Bash call that posted), typically within a second.

Fallback if async hooks don't fit (R1 fails): the delivery guard starts a detached `setsid` child with its stdio closed (spike R2). If neither works, sync runs only from listeners and `sync --follow`.

**Idle machines.** The async entry fires only when a session takes a turn. So, on a machine whose members are all idle:
- an armed listener (listeners spec) runs the same pull-due check on each poll, so pulled lines reach it;
- otherwise `passnote sync --follow`, a foreground loop the human starts in a terminal, keeps pulling.

**Push.** Read `log.jsonl` from the push cursor. Push each line whose `sid` is a **local** member of this room, in order. Skip pulled lines (no echo), held-state, events and anything else. Before each push:
- re-run `trust.looks_secret` on the text; a match is never pushed (§6.2);
- re-check the target's visibility (cached ≤ 10 min, §6.1).

Advance the push cursor after each successful push. A crash between the push and the cursor save pushes the line twice. Receivers dedupe by `(origin, id)`, so this is harmless.

**Pull.** Fetch records newer than `state.since` (conditional request, spike R3). For each record, in backend order:
- skip it if `rid` is in `seen_rids`, or its payload's `origin` is this machine;
- parse the payload; reject it if it isn't a valid message or control record;
- dedupe by `(origin, id)` against the last 256 KB of the local log;
- append it under the room lock with a fresh local seq, the fields of §4.3, and `verified` per §6.3.

The first-seen version of a record wins. Edits and deletions on the backend are ignored and logged as `remote-edit` events, so the human sees them in `watch`.

### 4.5 Identity across machines

- **Member records.** `join` in a remote room is online only. It pulls, then pushes a control record `{"t":"member","op":"join","name","alias","mkey","origin","label"}`, then pulls again to confirm it. `leave` and renames push `op: leave|rename`. Control records are never delivered to a model. They update `members.json` and show in `who` and `watch`.
- **Uniqueness.** Names and aliases are unique room-wide, first writer wins in remote order (Q4). If the confirming pull shows an earlier record with the same name, `join` fails with exit 2: "name X is used by a member on <label>". An alias clash is retried automatically with the next free alias.
- **Display.** `who` shows a remote member as `name @label` with state `remote` and the time of its last line. Warm/cold is unknown and is never guessed.
- **Takeover** of a remote member's name is refused (Q7). A remote member that leaves frees its name. One that vanishes without leaving is listed `remote, silent Nd` and can be removed only by `passnote remote forget <name>`, run by a human (§6.1).

### 4.6 The `github-issue` backend (Phase 2)

- **Target:** `github:<owner>/<repo>#<issue>`. The repo must be private (§6.1).
- **One comment per line.** The body has a human-readable part and a machine payload:
  ```
  `b112` **session-b** → session-a · ask
  split step 2?

  <!-- passnote v1 {"from":"session-b","id":"b112","kind":"ask",…} -->
  ```
  Only the payload is parsed. The visible part is decoration for people reading the issue. The payload is `json.dumps(ensure_ascii=True)` with `<`, `>` and `&` written as `<`, `>` and `&`, so no text can close the HTML comment.
- **Pull:** list the issue's comments since `state.since`, sending the last ETag. Confirm the `since` semantics (updated vs created time) and the rate-limit treatment of a 304 in spike R3; don't build on remembered numbers.
- **Push:** create one comment per line. Rate-limit and abuse responses (403/429, `Retry-After`) set `suspended_until` and are shown in `doctor`, `who` and `watch`.
- **Auth:** the human's existing `gh` login. passnote never stores a token. Spike R4 picks between calling `gh api` as a subprocess and fetching `gh auth token` into memory for each sync.
- The `author` of each comment comes from the API response, never from the payload.

A small HTTP relay with the same interface is Phase 4 and only if asked for (Q1).

### 4.7 CLI and config

| Command | Purpose |
|---|---|
| `remote attach <room> <target> [--label L]` | Bind a room to a backend. Human-only (§6.1). Pulls, writes `remote.json`, pushes a `room` record on first attach |
| `remote new <room> github:<owner>/<repo> [--label L]` | Create the issue, then attach |
| `remote detach <room>` | Stop syncing. Says plainly that nothing is deleted from the backend |
| `remote status [room]` | Backend, target, visibility, last pull and push, outbox depth, suspension |
| `remote forget <name> [--room r]` | Remove a silent remote member's record. Human-only |
| `sync [--room r] [--follow]` | One push and pull, used by the async hook and by listeners; safe to run by hand. `--follow` repeats every `pull_seconds` until interrupted |

Existing commands:
- `post` in a remote room prints `ok <id> (remote: pushes on your next tool call)`. For each remote addressee it prints `WAIT <name> remote (@label; delivered on its next turn)` instead of WAKE.
- `who` and `watch` mark remote members and pulled lines (`@label`, `(unverified)`).
- `doctor` gains one line per room: `room r: local`, or `room r: github-issue owner/repo#N, private, pulled 12s ago, pushed 3s ago, outbox 0`, plus failures with fix lines (#24 acceptance).

**Settings.**

| Key | Where | Default | Notes |
|---|---|---|---|
| `remote_pull_seconds` | room or global config | 60 | Minimum 15. Not security-relevant, so config may set it |
| `PASSNOTE_REMOTES` | launch env only | unset | Comma-separated allowlist of targets or target prefixes (`github:org/repo`). Sync and attach refuse any target not on it |
| `PASSNOTE_ALLOW_REMOTE_BYPASS` | launch env only | unset | `1` lets verified remote lines reach a non-prompting receiver (§6.3) |
| `PASSNOTE_REMOTE_AUTHORS` | launch env only | unset | Optional allowlist of backend principals. When set, records from other authors are not imported at all |

Security switches live only in the launch environment, never in config files, because the model can write config files (design §9).

## 5. Wakes

- A post never prints a WAKE line for a remote addressee: SendMessage can't cross machines.
- A pulled line that would wake a local member (design §8 eligibility, or a wake rule) is handed to that machine's listener, if one is armed (listeners spec). Without one, it waits for the member's next turn.
- The wake breaker counts per (sender, addressee) whatever the machine.

## 6. Trust and safety

The threat model widens. Locally, every room writer is a process of the same OS user (design §9). With a remote backend, **every principal that can write to the backend can write lines into the room, and every principal that can read it can read the whole room, forever.**

### 6.1 Who can read and write a remote log, and how a room opts in

- **Readers** of a `github-issue` room are everyone with read access to the repo: collaborators, org owners, installed apps with issue access, and GitHub itself. Comment edit history keeps old text. Detaching deletes nothing. `remote attach` prints this list in plain words before it writes anything.
- **Public targets are refused**, at attach and before every push batch (visibility cached ≤ 10 min). A repo made public later suspends pushes and shows in `doctor`, `who`, `watch` and every member's next systemMessage (Q5).
- **Opt-in is human-only.** Two gates, both needed:
  1. the target matches `PASSNOTE_REMOTES` in the launch environment of the process that syncs. The async hook inherits Claude Code's environment, which the model can't change;
  2. `remote attach`, `remote new` and `remote forget` refuse to run unless stdin is a TTY and `CLAUDECODE` is unset, so they run from the human's terminal through the `shim`.

  Honest limit (for the README): a model with unrestricted Bash can copy a room anywhere with `cat` and `gh`, and can unset variables for its own commands. What protects against that is Claude Code's permission prompt, as in design §9. The skill's recommended allowlist must never include `passnote remote *` or `passnote sync`. What passnote guarantees is narrower: its automatic paths (the async sync hook) never copy a room anywhere the human didn't configure.

### 6.2 Secret guard

- In a remote room, `post` refuses secret-looking text **with no override**: `--allow-secret-looking` is rejected (exit 5, "secret-looking text can't go to a remote room") (Q8).
- The pusher re-runs `looks_secret` on every line, because any same-user process can append to `log.jsonl` without `post`. A match is not pushed. It stays local, a `push-refused` event is appended, and the human sees a systemMessage.
- The skill adds: no local paths in remote rooms (they don't resolve on the other machine and leak layout), and no file references for long payloads.

### 6.3 Holds for pulled lines: fail closed

A pulled line's `mode` stamp is asserted by whoever wrote the record. `trust.mode_class` treats a missing or unknown mode as non-prompting. Without a new rule, a forged or unstamped remote line would be **delivered to a bypass or auto receiver**. So:

| Pulled line | Prompting receiver | Non-prompting receiver |
|---|---|---|
| verified, stamped prompting | deliver (class rule) | hold (class mismatch) |
| verified, stamped non-prompting | hold (class mismatch) | hold, unless the receiver was launched with `PASSNOTE_ALLOW_REMOTE_BYPASS=1` |
| unverified, or unstamped | hold | hold |

- **Verified** means the backend-authenticated `author` is the principal that pushed the first member record for that `origin`. The first record per origin binds origin to principal, first writer wins. A later record from another principal claiming the same origin is unverified.
- `PASSNOTE_ALLOW_BYPASS=1` does **not** cover remote lines. It covers local members, which run as the same OS user; remote writers are a wider set (Q3).
- The displayed sender comes from `members.json` (synced member records); a mismatch with `from` renders `(unverified)`, as today.
- Escaping and the one-line rendering invariant (design §7, A4) apply unchanged. Pulled text is untrusted input like any other.
- Holds keep text out of a model's context. They do not make the remote log confidential: every reader of the backend sees held lines.
- A new remote member joining shows every local member's human a systemMessage: `passnote[r]: X @label joined (author L)`.

## 7. Cost per hook fire

| Situation | Extra cost |
|---|---|
| Any session with the plugin, joined or not, in local rooms only | hook entries are static, so the new async entry spawns one extra `/bin/sh` per `UserPromptSubmit` and `PostToolBatch`; it exits on a missing marker. 0 tokens; Phase C measures the milliseconds |
| Joined to a remote room, nothing due | the async guard does one `stat` per remote room and exits. No Python, no network, 0 tokens |
| Pull due | one conditional request per room per machine per `pull_seconds`, off the turn's critical path. 0 tokens |
| New remote lines | delivered by the unchanged delivery hook: payload + header, like a local line (design §1) |

Network budget: one room at 60 s is 60 pulls/hour per machine, plus one push per post. R3 checks this against the backend's limits at the busiest room rate seen in the field.

Latency: a reply posted on machine B reaches a busy member on machine A within about `pull_seconds` plus the time to its next tool call. An idle member waits for its next turn, or for its listener.

## 8. Failure modes

| Failure | Behaviour |
|---|---|
| Backend unreachable, or auth expired | Local posts still append and deliver locally. The push cursor stays, so the outbox is the unpushed tail of the log. `post` says `remote: queued`. `doctor` and `who` show the error and its age; the first failure per session shows one systemMessage |
| Rate limited or abuse-flagged | `suspended_until` from the response; no retries before it |
| Target made public, deleted, locked or moved | Sync suspends and tells the human; nothing is pushed until it is re-attached |
| Crash between push and cursor save | Duplicate remote record, deduped by `(origin, id)` on pull |
| Crash between pull-append and state save | Re-fetched, deduped by `rid` and `(origin, id)` |
| Two machines claim one name at once | First in remote order wins; the loser's `join` fails |
| Two claims for one piece of work from two machines | Both are delivered; `who` orders claims by remote order and marks the later one `contested` |
| Clock skew | `ts` is display only. Order is backend order on pull and local order on the local log |
| Local log replaced or restarted (design §7) | The push cursor resets like a delivery cursor; duplicates are deduped on the other side |
| A record edited or deleted on the backend | Ignored; a `remote-edit` event shows in `watch` |
| No session on a machine takes turns | Nothing syncs until a listener is armed, `sync --follow` runs, or a session takes a turn (§4.4) |

## 9. Spikes (run before the plan)

Each runs on macOS and Linux against the minimum Claude Code version, and its raw evidence goes to `prototype/spikes/` with local paths scrubbed.

| id | What it must measure | Decides |
|---|---|---|
| R1 | Whether `"async": true` is accepted on `UserPromptSubmit` and `PostToolBatch` plugin hook entries. Measure: added turn latency with a 3 s async hook (expect ≈0); its timeout ceiling; whether it is killed at turn end, at session exit, and when `-p` exits; whether overlapping instances run concurrently; whether its environment has network and the user's `gh` credentials; whether its output is discarded | §4.4 trigger |
| R2 | Fallback only if R1 fails: a sync hook that starts a `setsid` child with stdio closed. Does the hook return at once, does the child survive the hook's exit and its 5 s timeout, and is it killed with the session? | §4.4 fallback |
| R3 | GitHub issue comments API: does a conditional request with the last ETag return 304, and does a 304 reduce `X-RateLimit-Remaining`? Are `since` results filtered by update time (so edits come back)? Pull and push latency p50/p95 over 100 runs; secondary-limit responses when posting at 2× the busiest field rate; the maximum comment body size | §4.6, §7 |
| R4 | `gh` auth from a hook process without a TTY: macOS keychain and Linux `GH_TOKEN`/hosts file. Compare the cost of `gh api` per call with `gh auth token` once plus `urllib` | §4.6 auth |
| R5 | Sandboxed Bash: `passnote post` in a remote room needs only the existing `allowWrite` entry. Time from `post` returning to the record existing on the backend, triggered by the sender's own next `PostToolBatch` | §4.1, §7 |
| R6 | End to end on two machines: an ask on A, then B's next turn. Measure delivery p50/p95 with B busy and with B idle, and confirm hook delivery on both sides | #24 acceptance |

## 10. Open questions (recommendations marked)

1. **First backend: GitHub issue or HTTP relay?** *Recommended:* a GitHub issue in a private repo. It is what the field team already used, it needs no infrastructure, and auth is the human's `gh` login. Keep a backend interface so a relay can follow if asked for; don't ship a server in v1.
2. **What runs sync: async hooks or a resident daemon?** *Recommended:* async hook entries, rate-limited by an mtime stamp, plus armed listeners and the human-started `sync --follow` loop for machines with no active sessions. This keeps ADR-0001's no-daemon decision. Gate it on R1.
3. **Remote lines into non-prompting receivers.** *Recommended:* always hold, unless the receiver was launched with `PASSNOTE_ALLOW_REMOTE_BYPASS=1`, a variable separate from `PASSNOTE_ALLOW_BYPASS`. Unverified or unstamped remote lines are held from everyone.
4. **Names across machines: plain and unique, or `name@machine`?** *Recommended:* plain names, unique room-wide, first writer wins in remote order. `who` shows `@label`. Addresses stay short, and the skill doesn't change.
5. **Public repos.** *Recommended:* refuse outright, with no flag. A public room is a published log of a team's working context.
6. **How a room opts in.** *Recommended:* the launch-env allowlist `PASSNOTE_REMOTES`, plus TTY-only `remote attach` run by the human. Config files can't enable a remote.
7. **Takeover across machines.** *Recommended:* not in v1. A remote member that leaves frees its name; a silent one is removed by a human with `remote forget`.
8. **`--allow-secret-looking` in remote rooms.** *Recommended:* rejected. The override exists for local rooms, where the reader set is one OS user.
9. **Threads (#25) in remote rooms.** *Recommended:* the `thread` field travels like any other message field, and subscriptions stay local per member. No backend change.

## 11. Implementation outline

**Phase 0: spikes R1–R6.** Fold the results into §4.4, §4.6 and §7.

**Phase 1: a remote-ready core, with no network.**
- Add `machine.json`, member keys, the §4.3 fields, the `remote:` member state (excluded from takeover, gc and the "gone" wake path), pull-append with a local seq and the origin id kept, the push cursor, the §6.3 hold table and the push-side secret rescan.
- A test-only `dir` backend: a directory standing in for the remote log, enabled only under the test suite.
- *Acceptance:*
  - Two `PASSNOTE_HOME`s on one machine, joined through the `dir` backend, exchange an `ask` and an `ans` with hook delivery on both sides, and `re` resolves across them.
  - Every row of the §6.3 table is a unit test. An unstamped remote line is held from every receiver.
  - A local `join --as <remote member's name>` fails. `gc` keeps remote members while attached.
  - A session in local rooms only does no extra file opens per delivery fire (a test counts them).
  - The rendering fuzz test (design §12) covers pulled lines.

**Phase 2: the `github-issue` backend.**
- Add the async sync hook entry and its guard, `remote attach|new|detach|status|forget`, `PASSNOTE_REMOTES`, visibility checks, ETag pulls, rate-limit suspension, and the `doctor`, `who` and `watch` lines.
- *Acceptance:*
  - #24: two sessions on different machines join one room and exchange asks and replies, with hook delivery on both sides.
  - The remote backend is opt-in per room, and `doctor` names each room's backend.
  - Attach refuses a public repo, a target not in `PASSNOTE_REMOTES`, and a run from inside Claude Code.
  - With a fake backend and a fake clock, ten sessions on one machine cause at most one pull per `pull_seconds`.
  - Secret-looking text is never pushed, including a line appended to `log.jsonl` directly.

**Phase 3: idle receivers.** Depends on the listeners spec, Phase L2 or later.
- A pulled line that is eligible to wake a local member goes to that member's listener. Armed listeners run the pull-due check on each poll.
- *Acceptance:* an idle, warm member on machine B with a listener armed is woken by an ask from machine A, with no SendMessage and no human action, within `pull_seconds` plus the listener's coalescing window.

**Phase 4 (only if asked for): HTTP relay backend.** The same interface, with a per-room bearer token from the launch environment.
