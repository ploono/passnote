# passnote: rooms across machines

**Status:** draft v0.2 (2026-10-09), for maintainer review. Part of #24. Not approved.
- v0.2 follows maintainer feedback: a NATS server, not a GitHub issue, carries cross-laptop rooms. NATS JetStream is now the first backend. The GitHub issue backend is demoted to a fallback (§4.9).

Builds on the Phase A design (`2026-09-26-passnote-design.md`, cited as "design §N") and its review (`2026-09-27-passnote-spec-review.md`, finding ids F1…F43). Waking idle members is the job of the listeners spec (`2026-10-09-passnote-listeners.md`). This spec depends on it for the follower in §4.5 and the "idle receivers" phase.

**ADR note.** This reopens a non-goal (design §2: "cross-machine rooms") and the scope note in ADR-0001 ("a resident daemon … only pays off for cross-machine rooms, which are out of scope"). It keeps ADR-0001's decision itself: the hook delivers from a plain local file log. passnote ships no resident daemon; the long-lived NATS connection in §4.5 belongs to a session's listener or to a loop the human starts. If this spec is approved, record a new ADR, "a room backend syncs a remote log into the local one".

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
- The first backend is a NATS JetStream stream per room. The backend interface stays pluggable.
- Remote rooms are opt-in per room, by a human, and `doctor` says which backend each room uses (#24 acceptance).
- Holds and the secret guard apply across machines, and they fail closed.
- A hook fire costs nothing extra when nothing is new: no network, no tokens, and at most one `stat` per remote room.
- passnote stays stdlib-only Python 3.9 with no required binaries.

**Non-goals.**
- Waking a member on another machine directly. A post never rings a doorbell across machines (§5). Remote wakes come from a listener on the receiving machine (listeners spec).
- Running or hosting a NATS server. The team brings one (§4.8). passnote documents a minimal server config and the permissions each machine needs.
- A user-level service (launchd or systemd) shipped by passnote (Q5).
- Sharing a `PASSNOTE_HOME` over a network filesystem. That stays unsupported (design §4, F39).
- Moving a member between machines. Takeover stays within one machine (Q9).
- End-to-end encryption of message text. TLS protects the wire. The server and its operator can read the stream (§6.1).

## 3. Proposed vocabulary (for CONTEXT.md once approved)

- **Room backend**: where a room's log is shared beyond this machine. `local` (the default, as today), `nats`, or a fallback such as `github-issue`.
- **Remote log**: the backend's copy of the room's log; for `nats`, one JetStream stream. Every machine syncs to and from it.
- **Sync**: copying new local lines to the remote log (push) and new remote lines into the local log (pull).
- **Follower**: a process that keeps one connection to the backend open and syncs as lines arrive. It is a session's listener, or `passnote sync --follow` started by the human.
- **Machine**: one `PASSNOTE_HOME`. It has a random machine id and a human-chosen label.
- **Remote member**: a member whose session runs on another machine. From this machine's point of view it is neither running nor gone; it is *remote*.
- **Origin**: the machine a message was first posted on.

## 4. Design

### 4.1 Shape

```
machine A                                         machine B
 session ─post─► rooms/r/log.jsonl ─push─►  JetStream stream  ─pull/follow─► rooms/r/log.jsonl ─hook─► session
 session ◄─hook─ rooms/r/log.jsonl ◄─pull/follow─  PN_<key>    ◄─push─ rooms/r/log.jsonl ◄─post─ session
```

- `post` appends to the local log first, as today, so delivery to members on the same machine never depends on the server. It then touches `remote/push-wanted`. If it can reach the server (a 1 s budget; see N4 for the sandbox), it also publishes the line directly. Otherwise the line is pushed by the sync. A double publish is harmless (§4.4 dedupe).
- Sync runs outside the delivery hook: in an async hook entry when no follower is running, or in a follower (§4.5). Either way, it writes pulled lines into the local log through the normal append path.
- The delivery hook is unchanged: it reads the local log from its cursor. A pulled line is delivered on the receiver's next turn, like any other line. **A NATS push never reaches a session directly.** It lands in the local file, and the hook picks it up.

### 4.2 Local layout additions

```
machine.json                          {machine_id (random 128-bit hex), label}
rooms/<room>/remote.json              {backend, target, room_key, pull_seconds}: a convenience copy, never authorization (§6.1)
rooms/<room>/remote/state.json        {last_stream_seq, last_pull, last_push, last_error, suspended_until, verifiable}
rooms/<room>/remote/push.json         push cursor {ino, off, seq} over log.jsonl
rooms/<room>/remote/push-wanted       touched by post; its mtime is newer than last_push when a push is due
rooms/<room>/remote/pulled            mtime = start of the last pull; the async guard's stat
rooms/<room>/remote/.sync.lock        one sync per room per machine (LOCK_NB; busy means skip)
remote/follower.pid                   {pid, rooms, started_at}, written by a running follower
sessions/<sid>/remote                 marker: this session is joined to at least one remote room
```

**Attachment records live outside `PASSNOTE_HOME`**, in `${XDG_CONFIG_HOME:-~/.config}/passnote/attachments.json` (0600): `{room: {target, room_key, attached_at}}`. Only the TTY-gated `remote attach` and `remote new` write it. Sandboxed Bash can't write it, because it is not in the `allowWrite` entry that `PASSNOTE_HOME` needs (§6.1).

Credentials are never stored here. They are named by launch-env variables (§4.7), so a room's config can't point sync at someone else's key.

### 4.3 Message and member fields

**Message fields added** (all optional, so `store.valid_message` accepts old lines):

| Field | On | Meaning |
|---|---|---|
| `origin` | every line in a remote room | machine id of the origin, short form (first 12 hex) |
| `mkey` | every line in a remote room | the sender's **member key**: 16 random hex chars minted at `join`, stored in the local `members.json` entry and moved by `carry_over` like `alias`, so it survives `/clear`. Never derived from the sid. Replaces `sid` on the wire |
| `rid` | pulled lines | the backend's record id: the JetStream stream sequence |
| `author` | pulled lines | the backend-authenticated principal: for `nats`, the machine token in the subject the server accepted (§6.3) |
| `verified` | pulled lines | `true` only when `author` matches `origin` and the room is verifiable (§6.3) |

**What changes for pulled lines.** These traps come from the code in `store.py`, `hook.py`, `rooms.py` and `wake.py`.
- **seq is local.** `append_message` assigns `seq = last_seq + 1` under the local room lock, and `_read_room` skips any line whose seq is below the cursor's as forged. A pulled line is appended through the local lock with a **fresh local seq**. Its origin seq is never written into the local `seq`.
- **id is global.** `re`, `read --id`, unanswered tracking and `last_emit` key on `id`. A pulled line keeps its **origin id verbatim**. That works only because aliases are unique across machines (§4.6). `append_message` gains a path that keeps a given id instead of minting `alias+seq`.
- **sid is never shared.** On the wire, `sid` is replaced by `mkey`. A pulled line's local `sid` is `remote:<mkey>`. Code that treats `sid` as a session UUID (`sessions.recorded_mode`, `paths.check_sid`) must treat the `remote:` prefix as "no local session" and never build a path from it.
- **Remote members are not gone.** `rooms.is_running` returns False for a sid with no local session dir. Today a remote member would therefore be taken over by a local newcomer, pruned by `gc` and `_sweep_orphan_members`, and reported "gone" by `wake.decide`.
  - Remote members are stored in `members.json` under `remote:<mkey>` with `remote: true, machine, author`.
  - Every one of those paths treats them as a third state: no takeover, no gc while the room is attached, and `WAIT … remote` in `post`.

**Never leaves the machine:** `sid`, `root`, `transcript_path`, `permission_mode` history, cursors, `events.jsonl`, holds and `errors.log`. The wire carries only:
- the message fields of design §6, minus `sid`;
- `origin` and `mkey`;
- the control records of §4.6.

### 4.4 The `nats` backend

**Core NATS or JetStream?** JetStream (Q1). Core NATS is fire-and-forget: a laptop that is asleep or offline misses every message posted meanwhile, and a new member can't replay anything. A room is an ordered, append-only, replayable log with one cursor per reader, which is what a JetStream stream is.

**One stream per room**, created once by whoever administers the server (`remote new`, §4.7):

| Stream setting | Value | Why |
|---|---|---|
| name | `PN_<room_key>` | `room_key`: 16 random hex chars, so neither the stream name nor any subject leaks a project name |
| subjects | `pn.<room_key>.msg.*`, `pn.<room_key>.ctl.*` | the last token is the origin machine token (§6.3) |
| storage, retention | file, limits | |
| `max_age` | 7 days (Q8) | matches `gc`'s 7 days for gone members; the log doesn't live forever |
| `max_msg_size` | 16 KB | 4,000 characters of text (design §6), escaped, plus fields |
| `duplicate_window` | 10 min | the server dedupes retried publishes by `Nats-Msg-Id` |
| `deny_delete`, `deny_purge` | true | append-only, like the local log. Nothing can be edited |
| `allow_direct` | true | cheap reads by sequence for `remote status` and spikes |

**Push.** One JetStream publish per local-origin line, to `pn.<room_key>.msg.<machine>`, with the header `Nats-Msg-Id: <origin>-<id>`. The publish waits for the server's PubAck and then advances the push cursor. A retry after a lost ack is deduped by the server inside the window, and by `(origin, id)` on pull after it.

**Pull.** An ephemeral pull consumer (`deliver_policy: by_start_sequence`, `opt_start_seq: last_stream_seq + 1`, `ack_policy: none`, a short `inactive_threshold`), then `CONSUMER.MSG.NEXT` requests:
- **one-shot sync** (async hook): fetch a batch with `no_wait`, append it, close;
- **follower**: long-poll fetches (`expires` about 30 s), which return as soon as a message arrives. That gives push latency with no push-consumer flow control.

passnote keeps the cursor itself (`last_stream_seq`). Server-side durable consumers aren't needed, so a machine needs no permission to create durable state. The exact API subjects and the minimum server version are pinned by spike N2.

**Each pulled record:**
- skip it if its stream seq is at or below `last_stream_seq`, or its subject's machine token is this machine;
- parse the payload; reject it if it isn't a valid message or control record;
- dedupe by `(origin, id)` against the last 256 KB of the local log;
- append it under the room lock with a fresh local seq, the fields of §4.3, and `verified` per §6.3;
- save `last_stream_seq` after the append. A crash in between re-fetches the record, and the dedupe drops it.

**How passnote talks to NATS** (Q2). Three options:

| Option | For | Against |
|---|---|---|
| **A minimal stdlib client** (recommended) | No new dependency. The NATS protocol is text over TCP (`INFO`, `CONNECT`, `PUB`/`HPUB`, `SUB`, `MSG`/`HMSG`, `PING`/`PONG`, `+OK`/`-ERR`), and TLS is in `ssl`. JetStream is JSON request/reply on `$JS.API.*` subjects. passnote needs about six calls: stream info, consumer create, fetch, publish with PubAck, direct get, and a probe. A pull-only design avoids push consumers, flow control and heartbeats. Estimated 400–600 lines plus tests | passnote owns protocol code. **NKey and JWT/creds auth need Ed25519 signing, which Python's stdlib lacks**, so the stdlib client can do TLS with client certificates (mTLS), user/password or a token, but not creds files (Q4) |
| Shell out to the `nats` CLI | Every auth method, creds included. No protocol code | An extra binary on every machine. One process and TLS handshake per call. CLI output isn't a stable API. A follower would have to parse a streaming `nats` subprocess |
| A sidecar (a local `nats-server` leaf node, or a small bridge) | Reconnects, creds and TLS handled by NATS itself. passnote talks plaintext to localhost | A resident process passnote would install and supervise, against ADR-0001. JetStream across leaf nodes adds domains to configure |

**Recommended:** the stdlib client, with mTLS as the documented auth, plus user/password over TLS. Creds (needed by hosted NATS services that issue them) are not in v1. If hosted support is wanted, add an optional `nats` CLI transport behind the same interface rather than vendoring Ed25519 code (Q4).

### 4.5 Who syncs, and when (push meets the near-zero-cost hook)

A NATS server can push, but only to a process that holds a connection open, and hooks are short-lived. Two runners share one sync function and one lock (`.sync.lock` per room), so they never pull at the same time:

1. **Async hook entry** (no long-lived process). A second hook entry on `UserPromptSubmit` and `PostToolBatch` with `"async": true`, so it never delays the turn (spike R1). Its sh guard:
   1. exits unless `sessions/$sid/remote` exists (sessions in local rooms only stop here, with one file test);
   2. exits if `remote/follower.pid` names a live process, because the follower already syncs;
   3. for each remote room, `stat`s `remote/pulled` and `remote/push-wanted`, and exits unless a pull is due (`now - mtime(pulled) >= pull_seconds`) or a push is due;
   4. runs `passnote sync --room <r>`: connect, push, one `no_wait` fetch, close.
   
   A machine pulls each room at most once per `pull_seconds`, however many sessions it runs. A post is pushed by the sender's own next `PostToolBatch` (the one that ends the Bash call that posted).
2. **Follower** (push latency). One connection per machine, long-polling every remote room. It also watches `push-wanted` (one `stat` per second) and pushes at once. It writes `remote/follower.pid`. Two kinds:
   - an **armed listener** (listeners spec, §4.4 there) is the follower while it waits. It is session-scoped and ends with its session;
   - **`passnote sync --follow`**, run by the human in a terminal for a machine whose sessions are all idle, or for lower latency.

With a follower, a line posted on machine B is in machine A's local log within network latency. A busy member gets it at its next tool call, and an idle one when its listener wakes it. Without a follower, latency is up to `pull_seconds` plus the time to the next turn.

Added to `hooks/hooks.json`, one entry per event, alongside the existing synchronous ones:
```json
{"type": "command", "command": "/bin/sh", "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/guard.sh", "Sync"], "async": true, "timeout": 30}
```
`guard.sh` needs a new `Sync` case arm. Today it ends in `*) exit 0`, so an unknown event name silently does nothing.

Fallback if async hooks don't fit (R1 fails): the delivery guard starts a detached `setsid` child with its stdio closed (spike R2). If neither works, sync runs only in followers.

### 4.6 Identity across machines

- **Member records.** `join` in a remote room is online only.
  1. It pulls.
  2. It publishes a control record `{"t":"member","op":"join","name","alias","mkey","origin","label"}` to `pn.<room_key>.ctl.<machine>`.
  3. It pulls again to confirm.

  `leave` and renames publish `op: leave|rename`. Control records are never delivered to a model. They update `members.json` and show in `who` and `watch`.
- **Uniqueness.** Names and aliases are unique room-wide. The first writer in stream order wins (Q7).
  - If the confirming pull shows an earlier record with the same name, `join` fails with exit 2: "name X is used by a member on <label>".
  - An alias clash is retried automatically with the next free alias.
- **Display.** `who` shows a remote member as `name @label`, with state `remote` and the time of its last line. Warm/cold is unknown and is never guessed.
- **Takeover** of a remote member's name is refused (Q9).
  - A remote member that leaves frees its name.
  - One that vanishes is listed as `remote, silent Nd` until a human runs `passnote remote forget <name>`, or until its records age out of the stream.

### 4.7 CLI and settings

| Command | Purpose |
|---|---|
| `remote new <room> nats://<host>[:port] [--label L]` | Server admin only. Creates the stream (§4.4) and prints the `room_key`, the attach target, and a permissions snippet for each machine user (§6.3). Human-only (§6.1) |
| `remote attach <room> nats://<host>[:port]/<room_key> [--label L]` | Bind a room to the stream. Human-only. Runs the permission probe (§6.3), pulls, and writes `remote.json` |
| `remote detach <room>` | Stop syncing. Says plainly that nothing is deleted from the stream; records expire after `max_age` |
| `remote status [room]` | Backend, target, TLS and auth in use, verifiable or not, last pull and push, outbox depth, follower |
| `remote forget <name> [--room r]` | Remove a silent remote member's record locally. Human-only |
| `sync [--room r] [--follow]` | One push and pull, safe to run by hand. `--follow` runs a follower until interrupted |

Existing commands:
- `post` in a remote room prints `ok <id> (remote: pushed by your next tool call)`, or `(remote: follower pushes now)`. For each remote addressee it prints `WAIT <name> remote (@label; delivered on its next turn)` instead of WAKE.
- `who` and `watch` mark remote members and pulled lines (`@label`, `(unverified)`).
- `doctor` gains one line per room: `room r: local`, or `room r: nats <host>, TLS, mTLS, verifiable, pulled 12s ago, outbox 0, follower: listener`. Failures get fix lines (#24 acceptance).

**Settings.** Security switches and credentials live only in the launch environment, never in config files, because the model can write config files (design §9).

| Key | Where | Default | Notes |
|---|---|---|---|
| `remote_pull_seconds` | room or global config | 60 | Minimum 15. Applies only without a follower. Not security-relevant |
| `PASSNOTE_REMOTES` | launch env | unset | Comma-separated allowlist of targets or prefixes (`nats://nats.team.internal`). Sync and attach refuse anything else |
| `PASSNOTE_NATS_CERT`, `PASSNOTE_NATS_KEY`, `PASSNOTE_NATS_CA` | launch env | unset | mTLS client cert, key and CA bundle paths |
| `PASSNOTE_NATS_USER`, `PASSNOTE_NATS_PASSWORD_FILE` | launch env | unset | user/password auth, as an alternative to mTLS |
| `PASSNOTE_ALLOW_REMOTE_BYPASS` | launch env | unset | `1` lets verified remote lines reach a non-prompting receiver (§6.3) |

### 4.8 Hosting

| | Self-hosted `nats-server -js` (recommended) | A hosted NATS service |
|---|---|---|
| Setup | One static binary and a config file with TLS, users and permissions. Reachable over the team's private network (VPN or tailnet), or on a public host with TLS | An account and per-machine credentials |
| Auth from stdlib passnote | mTLS (`verify_and_map`) or user/password | Hosted services generally issue creds (JWT + NKey): needs Ed25519, so not with the v1 stdlib client (Q4; spike N3 confirms) |
| Who can read | The operator, anyone with disk access to the server (unless the server encrypts at rest), and every user granted the room's subjects | The same, plus the provider |
| Recommended | Yes, documented in the README with a minimal config | Later, with a creds-capable transport |

### 4.9 Alternatives behind the same interface

The backend interface is four calls: `publish(record) -> rid`, `fetch(after_rid, wait) -> [records with authenticated author]`, `probe() -> verifiable?`, and `describe() -> doctor line`.

| Backend | Fit as a room log | Stdlib client | Identity binding | Verdict |
|---|---|---|---|---|
| **NATS JetStream** | Ordered, replayable, append-only (`deny_delete`), retention by age | Text protocol, about 500 lines | Per-subject publish permissions bind the origin machine | First |
| Redis Streams (`XADD`, `XREAD BLOCK`) | Ordered and replayable, trimmed by `MAXLEN`/`MINID`, entries immutable | RESP is trivial, about 200 lines; TLS via `ssl` | ACLs are per key, not per entry. Binding needs one stream per machine per room, merged on read | Strong runner-up if a team already runs Redis |
| MQTT 5 (persistent sessions, QoS 1) | A per-client session queue, not a log. Retained messages keep only the last per topic, so there's no history for a new member | A binary protocol, about 400 lines | Per-topic ACLs bind the origin like NATS | Weaker: no replay |
| GitHub issue comments (v0.1's first backend) | Ordered and durable. Comments are editable, so the first-seen version wins | `urllib` plus a token from `gh auth token` | The comment author's login | **Fallback** for teams with no server. Private repos only; public repos are refused. ETag polling, with no push. The machine payload sits in an HTML comment, with every literal `<`, `>` and `&` replaced by the JSON escapes `\u003c`, `\u003e` and `\u0026` after `json.dumps(ensure_ascii=True)`, so no text can close the comment early (`json.dumps` alone leaves `-->` intact) |

### 4.10 NATS for local rooms too?

This records the maintainer's direction and how it fits.

**Per-room backend.** Each room has a backend: `local` (the default) or `nats`. When NATS is configured on the machine, `nats` could become the default for new rooms, even rooms whose members are all on one laptop. The reasons:
- one code path for every room;
- push wakes from the follower;
- JetStream sequencing and replay;
- a room can go cross-machine later without migration.

**Invariant, whatever the backend:** the delivery hook reads **only** the local file log and never touches the network. So:
- a hook fire with nothing new stays near-zero cost (§7);
- a NATS server that is down or slow never stalls or times out delivery.

In a `nats` room:
- `post` publishes to NATS (§4.1);
- a sync process mirrors the stream into the local log;
- that process is the natural home for doorbells and for the #14 listener, because it sees each line as it arrives (§5; listeners spec §4.4).

This spec deviates in one detail: `post` also appends its own line to the local log *before* publishing. A NATS outage then never delays or loses delivery to members on the same machine, and the mirror skips lines whose origin is this machine. The other direction of the maintainer's direction holds as stated.

**What NATS costs a room with only local members:**
- **A required moving part.** A `local` room needs no process at all. A `nats` room needs a sync process (a follower, or the async hook's short pulls) to keep the stream current, and a follower for push wakes.
- **While sync isn't running:**
  - `post` still succeeds and delivers to local members at once. It prints `ok b12 (remote: queued; no sync running)`.
  - The line waits in the outbox (the log past the push cursor), and is pushed in order when sync returns.
  - The only losses are push wakes and the stream's copy.
- **What `doctor` and `who` show:**
  - `doctor` shows `room r: nats, sync not running (last push 14m ago, outbox 7)` with the fix line `passnote sync --follow, or passnote listen on in a member session`.
  - `who` adds one header line, `remote: outbox 7, sync down 14m`.
  - `watch` shows the same state live.
- **A new reader set.** Text that today never leaves the laptop becomes readable by the server's operator and by every user with grants on the room's subjects (§6.1). On a loopback `nats-server` this is the same OS user, which changes nothing. On a shared server it is a real change of who can read the room.
- **Going cross-machine later doesn't need it.** `remote attach` binds an existing `local` room in place: the cursors, ids and members stay. Lines from before the attach stay local unless `--backfill N` is given. So "no migration" doesn't require starting every room on NATS.

**Recommendation (Q12):**
- Keep `nats` opt-in per room in v1, with `join --backend nats` or `remote attach`.
- Allow a machine-wide default only through the launch-env variable `PASSNOTE_DEFAULT_BACKEND=nats`, honoured only when the target is in `PASSNOTE_REMOTES`. A config file must not be able to point rooms at a server.
- Revisit after Phase 3, once follower reliability and push-wake latency are measured.

## 5. Wakes

- A post never prints a WAKE line for a remote addressee: SendMessage can't cross machines.
- A pulled line that would wake a local member (design §8 eligibility, or a wake rule) is handled by that machine's listener, if one is armed. The listener is usually the follower that pulled it, so the wake follows within its coalescing window. Without a listener, the line waits for the member's next turn.
- The wake breaker counts per (sender, addressee), whatever the machine.

## 6. Trust and safety

The threat model widens. Locally, every room writer is a process of the same OS user (design §9). With a remote backend:
- **every credential allowed to publish on the room's subjects can write lines into the room;**
- **every credential allowed to read them, plus the server's operator, can read the whole room until it expires.**

### 6.1 Who can read and write, and how a room opts in

- **Readers** of a `nats` room are every user the server grants the room's subjects or stream, the operator, and anyone with access to the server's disk. `remote attach` prints this in plain words before it writes anything.
- **TLS is required.** Plaintext is refused unless the host is a loopback address. Cert verification is on, with `PASSNOTE_NATS_CA` for a private CA.
- **Retention is bounded:** `max_age` 7 days by default. `remote detach` deletes nothing; it stops this machine syncing.
- **Opt-in is human-only.** Two gates, both needed:
  1. the target matches `PASSNOTE_REMOTES` in the launch environment of the syncing process. The async hook and listeners inherit Claude Code's environment, which the model can't change;
  2. `remote new`, `remote attach` and `remote forget` refuse to run unless stdin is a TTY and `CLAUDECODE` is unset, so they run from the human's terminal through the `shim`;
  3. every sync, whether hook, follower or by hand, authorizes a room only from its **attachment record** (§4.2). The record binds the local room name to the target and the `room_key`, and it is stored outside `PASSNOTE_HOME`, which model-run Bash commands may write. `rooms/<room>/remote.json` and the `sessions/<sid>/remote` marker are performance hints only: a room without a matching attachment record is never synced, whatever they say.

  `PASSNOTE_REMOTES` entries can name a whole server (`nats://host`) or a single stream (`nats://host/<room_key>`). The attachment record then decides which local room may use it. `doctor` warns when the attachments file is writable from the sandbox (its directory is inside an `allowWrite` entry), or is not 0600 and owned by the user.

  Honest limit, for the README: a model with unrestricted Bash can read the credentials and the room with ordinary commands. What protects against that is Claude Code's permission prompt, as in design §9. The skill's recommended allowlist must never include `passnote remote *` or `passnote sync`. passnote's own guarantee is narrower: its automatic paths never copy a room anywhere the human didn't configure.

### 6.2 Secret guard

- In a remote room, `post` refuses secret-looking text **with no override**. `--allow-secret-looking` is rejected with exit 5: "secret-looking text can't go to a remote room" (Q10).
- The pusher re-runs `trust.looks_secret` on every line, because any same-user process can append to `log.jsonl` without going through `post`.
  - A match is not pushed. It stays local.
  - A `push-refused` event is appended, and the human sees a systemMessage.
- The skill adds: no local paths in remote rooms (they don't resolve on the other machine, and they leak layout), and no file references for long payloads.

### 6.3 Verified senders and holds: fail closed

**Binding the origin.**
- Each machine is its own NATS user. Its publish permission covers only its own subjects: `pn.<room_key>.msg.<machine>` and `pn.<room_key>.ctl.<machine>`.
- Each user also gets the JetStream API subjects it needs, and a private inbox prefix (`_INBOX_<machine>.>`) so no machine can read another's replies.
- The server, not the payload, then vouches for the subject's machine token. That token becomes `author`.
- A pulled line is **verified** only when all of these hold:
  - `author` equals the payload's `origin`;
  - the member record for its `mkey` came from the same machine;
  - the room is **verifiable**.
- **Verifiable** means `remote attach` probed the server and found the binding enforced: a publish to another machine's subject was refused with a permissions violation, and a stream purge was refused.
  - A server that allows either marks the room unverifiable, and every pulled line in it is unverified.
  - `doctor` shows it with the permissions snippet as the fix.

**Holds.** A pulled line's `mode` stamp is asserted by the origin machine. `trust.mode_class` treats a missing or unknown mode as non-prompting. Without a new rule, a forged or unstamped remote line would be **delivered to a bypass or auto receiver**. So:

| Pulled line | Prompting receiver | Non-prompting receiver |
|---|---|---|
| verified, stamped prompting | deliver (class rule) | hold (class mismatch) |
| verified, stamped non-prompting | hold (class mismatch) | hold, unless the receiver was launched with `PASSNOTE_ALLOW_REMOTE_BYPASS=1` |
| unverified, or unstamped | hold | hold |

- `PASSNOTE_ALLOW_BYPASS=1` does **not** cover remote lines. It covers local members, which run as the same OS user. Remote writers are a wider set (Q6).
- The displayed sender comes from the synced member records. A mismatch with `from` renders `(unverified)`, as today.
- Escaping and the one-line rendering invariant (design §7, A4) apply unchanged. Pulled text is untrusted input like any other.
- Holds keep text out of a model's context. They don't make the stream confidential: every reader of the stream sees held lines.
- When a new remote member joins, every local member's human sees a systemMessage: `passnote[r]: X @label joined (machine M)`.

## 7. Cost per hook fire

| Situation | Extra cost |
|---|---|
| Any session with the plugin, in local rooms only (joined or not) | Hook entries are static, so the new async entry spawns one extra `/bin/sh` per `UserPromptSubmit` and `PostToolBatch`. It exits on a missing marker. 0 tokens; Phase C measures the milliseconds |
| Joined to a remote room, follower running | The async guard sees the live follower and exits: one more file test. 0 tokens, no network |
| Joined to a remote room, no follower, nothing due | One `stat` per remote room, then exit. No Python, no network, 0 tokens |
| Pull due, no follower | One connect (TCP + TLS), one `no_wait` fetch and close, per room per machine per `pull_seconds`. It runs off the turn's critical path. 0 tokens |
| Follower waiting | One open connection per machine, and a long-poll fetch every ~30 s per room. 0 tokens |
| New remote lines | Delivered by the unchanged delivery hook: payload + header, like a local line (design §1) |

Network budget: without a follower, one room at 60 s is 60 short connections per hour per machine, plus one publish per post. With a follower, it is one connection. N1 measures connect cost; if it's high, a follower becomes the recommended setup.

## 8. Failure modes

| Failure | Behaviour |
|---|---|
| Server unreachable, laptop asleep, or network changed | Local posts still append and deliver locally. The push cursor stays, so the outbox is the unpushed tail of the log. `post` says `remote: queued`. On reconnect, the pull resumes from `last_stream_seq`; JetStream keeps everything within `max_age`. `doctor` and `who` show the error and its age |
| Offline longer than `max_age` | The stream's first sequence is past `last_stream_seq`. Sync appends one `remote-gap` event, and `who`, `watch` and the systemMessage say lines were missed. It resumes from the first available record |
| Auth or cert expired | Sync suspends and tells the human. Nothing is retried in a loop |
| Permission violation on publish | The room is marked unverifiable and pushes stop. The fix line shows the permissions snippet |
| Lost PubAck, then retry | Deduped by `Nats-Msg-Id` within the window, and by `(origin, id)` on pull |
| Crash between pull-append and saving `last_stream_seq` | Re-fetched and deduped |
| Two machines claim one name at once | First in stream order wins; the loser's `join` fails |
| Two claims for one piece of work from two machines | Both are delivered. `who` orders claims by stream order and marks the later one `contested` |
| Clock skew | `ts` is display only. Order is stream order on pull, and local order in the local log |
| Local log replaced or restarted (design §7) | The push cursor resets like a delivery cursor. Duplicates are deduped on the other side |
| Follower dies | Its pidfile names a dead process, so the async guard takes over at the next turn |
| No session on a machine takes turns, and no follower | Nothing syncs until a listener is armed, `sync --follow` runs, or a session takes a turn |

## 9. Spikes (run before the plan)

Each runs on macOS and Linux against the minimum Claude Code version and a pinned `nats-server` version. Raw evidence goes to `prototype/spikes/`, with local paths and hostnames scrubbed.

| id | What it must measure | Decides |
|---|---|---|
| R1 | Whether `"async": true` is accepted on `UserPromptSubmit` and `PostToolBatch` plugin hook entries. Measure: the added turn latency with a 3 s async hook (expect ≈0); its timeout ceiling; whether it is killed at turn end, at session exit, and when `-p` exits; whether overlapping instances run concurrently; whether it has network access; whether its output is discarded | §4.5 runner |
| R2 | Fallback only if R1 fails: a sync hook that starts a `setsid` child with its stdio closed. Does the hook return at once? Does the child survive the hook's exit and its 5 s timeout, and is it killed with the session? | §4.5 fallback |
| N1 | **A throwaway stdlib client** (~300 lines): connect with TLS and mTLS, `HPUB` with `Nats-Msg-Id`, PubAck, an ephemeral pull consumer, `no_wait` and long-poll fetches. Measure: connect + TLS + fetch latency p50/p95 over a private network and over the internet; whether a long-poll returns within 100 ms of a publish; behaviour across laptop sleep and a network change; lines of code and the protocol features actually needed | Q2, §7 |
| N2 | **JetStream API and permissions** on the minimum server version: the exact subjects for consumer create and fetch; ephemeral consumer cleanup by `inactive_threshold`; `deny_delete`/`deny_purge`, `max_age`, the duplicate window and `allow_direct`. The minimum permission set for a machine user, and proof that with it a machine can't publish to another machine's subject, purge or delete the stream, read another room, or read another machine's inbox | §4.4, §6.3 |
| N3 | **Auth options without Ed25519:** mTLS with `verify_and_map`, and user/password over TLS. Whether the common hosted NATS services accept anything but creds. The cost per call of the `nats` CLI as a fallback transport | Q3, Q4 |
| N4 | **Sandbox:** sandboxed `passnote post` in a remote room works with only the existing `allowWrite` entry, falling back to the outbox. Can sandboxed Bash open a raw TCP + TLS connection to the NATS host at all, and with which `network` settings? The sandbox proxy may only carry HTTP and SOCKS. The hook-run sync (unsandboxed, A5) reaches the server. Time from `post` to PubAck by each path, with and without a follower | §4.1, §4.10, §7 |
| N5 | **End to end on two machines:** an ask on A, then B's next turn. Delivery p50/p95 with B busy, B idle with a listener, and B idle without one. Confirm hook delivery on both sides | #24 acceptance |

## 10. Open questions (recommendations marked)

1. **Core NATS or JetStream?** *Recommended:* JetStream, with one stream per room, a client-side cursor (the last stream sequence), and ephemeral pull consumers. Core NATS drops messages for any laptop that is offline or asleep, and gives no history to a new member.
2. **How passnote talks to NATS.** *Recommended:* a minimal stdlib client, pull-only (no push consumers, flow control or heartbeats), with mTLS or user/password over TLS. It keeps passnote stdlib-only with no extra binary. The cost is about 500 lines of protocol code that passnote owns. The `nats` CLI and a sidecar are rejected for v1: the CLI is an extra binary and parses unstable output; a sidecar is a daemon. Gate this on N1.
3. **Self-hosted or hosted NATS?** *Recommended:* document a self-hosted `nats-server -js` reachable over the team's private network or TLS, with one user per machine and the §6.3 permissions. Hosted services wait on Q4.
4. **NKey and creds support (hosted services).** *Recommended:* not in v1. If it's wanted, add an optional `nats` CLI transport behind the backend interface. Don't vendor Ed25519 signing code into passnote.
5. **Who holds the long-lived connection?** *Recommended:* only followers. That means an armed listener, or a `sync --follow` the human starts. Without one, the async hook runs short pulls. passnote ships no launchd or systemd unit; the README can show one as the user's own choice. This keeps ADR-0001's no-daemon decision.
6. **Remote lines into non-prompting receivers.** *Recommended:* always hold, unless the receiver was launched with `PASSNOTE_ALLOW_REMOTE_BYPASS=1`, separate from `PASSNOTE_ALLOW_BYPASS`. Unverified or unstamped lines, and every line in an unverifiable room, are held from every receiver.
7. **Names across machines.** *Recommended:* plain names, unique room-wide, first writer in stream order wins. `who` shows `@label`.
8. **Retention.** *Recommended:* `max_age` of 7 days, plus `deny_delete` and `deny_purge`, so the stream is append-only and doesn't keep a team's working context forever.
9. **Takeover across machines.** *Recommended:* not in v1.
10. **`--allow-secret-looking` in remote rooms.** *Recommended:* rejected.
11. **Threads (#25).** *Recommended:* the `thread` field travels like any other field. Subscriptions stay local. No backend change.
12. **Should `nats` be the default for local-only rooms when NATS is configured?** *Recommended:* no, opt-in per room in v1 (§4.10).
    - It adds a required sync process to rooms that need none.
    - It widens who can read text that today never leaves the laptop.
    - "Going cross-machine without migration" already works by attaching a local room in place.
    - A machine-wide default is allowed only through the launch-env variable `PASSNOTE_DEFAULT_BACKEND=nats`.
    - Revisit after Phase 3's follower data.

## 11. Implementation outline

**Phase 0: spikes R1, R2 and N1–N5.** Fold the results into §4.4, §4.5 and §7.

**Phase 1: a remote-ready core, with no network.**
- Add:
  - `machine.json`, member keys, the §4.3 fields, and the `remote:` member state (excluded from takeover, gc and the "gone" wake path);
  - pull-append with a local seq and the origin id kept;
  - the push cursor, the §6.3 hold table and the push-side secret rescan;
  - the backend interface (§4.9).
- A test-only `dir` backend stands in for the remote log, enabled only under the test suite.
- *Acceptance:*
  - Two `PASSNOTE_HOME`s on one machine, joined through the `dir` backend, exchange an `ask` and an `ans` with hook delivery on both sides, and `re` resolves across them.
  - Every row of the §6.3 table is a unit test, including the unverifiable-room case.
  - A local `join --as <remote member's name>` fails, and `gc` keeps remote members while attached.
  - A session in local rooms only does no extra file opens per delivery fire (a test counts them).
  - The rendering fuzz test (design §12) covers pulled lines.

**Phase 2: the `nats` backend, without followers.**
- Add:
  - the stdlib client and the async sync hook entry with its guard;
  - `remote new|attach|detach|status|forget`, `PASSNOTE_REMOTES`, the TLS requirement and the permission probe;
  - the `doctor`, `who` and `watch` lines.
- Integration tests run against a real `nats-server` when one is installed, and are skipped otherwise.
- *Acceptance:*
  - #24: two sessions on different machines join one room and exchange asks and replies, with hook delivery on both sides.
  - The remote backend is opt-in per room, and `doctor` names each room's backend.
  - Attach refuses plaintext to a non-loopback host, a target not in `PASSNOTE_REMOTES`, and a run from inside Claude Code.
  - A `remote.json` written by hand, with no matching attachment record, is never synced.
  - With N2's permission set the room is verifiable. With a permissive server it is not, and every pulled line is held.
  - Ten sessions on one machine cause at most one pull per `pull_seconds` (fake clock).
  - Secret-looking text is never pushed, including a line appended to `log.jsonl` directly.
  - A laptop offline for an hour catches up in order, with no duplicates.

**Phase 3: followers and idle receivers.** Depends on the listeners spec, Phase L2 or later.
- Add `sync --follow`, the listener as follower, `follower.pid`, and the guard skipping pulls while a follower is live.
- *Acceptance:*
  - With a follower, a line posted on machine A is in machine B's local log within 1 s (p95 on a private network).
  - An idle, warm member on B with a listener armed is woken by an ask from A, with no SendMessage and no human action.

**Phase 3b (if Q12 is decided for a default): `PASSNOTE_DEFAULT_BACKEND`.**
- *Acceptance:*
  - With it set and the target allowlisted, a new room is created as `nats`. Without the allowlist entry, the room is `local` and `doctor` says why.
  - Stopping the follower shows the §4.10 lines in `doctor` and `who`.
  - Posts made while sync is down are delivered locally at once, and reach the stream in order when sync returns.

**Phase 4 (only if asked for): alternatives** behind the same interface: the GitHub issue fallback, Redis Streams, or a creds-capable `nats` CLI transport (Q4).
