# passnote design spec: review

The spec was reviewed against 43 findings, and none was rejected. After verification there are no critical findings, 25 major, 15 minor and 3 nits. F1 and F2 were downgraded from critical to major. 21 findings held only in part; those are marked "contested".

## 1. Verdict

The spec is ready for an implementation plan once the fixes below are made; no redesign is needed. The core survived every review: an opt-in hook injects new lines from an append-only log per room, tracked by a cursor per session, with no daemon. But the 25 major findings touch almost every section, so this needs a proper revision, not a patch. Rewrite §3–§7 and §12 with the fixes and scope cuts below, run the new spikes, and then write the plan.

Five items change the design most, so settle them first:
- Hold peer messages to sessions running in bypass-permissions mode (F1).
- Fix the race between concurrent hooks on the same cursor (F3).
- Key identity on `CLAUDE_CODE_SESSION_ID` and handle session-id changes (F4, F24).
- Make the SendMessage doorbell the only wake mechanism in v1 (F6, F7).
- Limit what `dispatch` can read (F9).

The docs already answer five of the six §12 spikes, and several of their fallbacks are wrong (F33).

## 2. Must-fix before planning

For contested findings, the fix shown already includes the verifier's correction.

### Trust and security

**F1: Hook delivery bypasses Claude Code's cross-session inbound controls** (§4–§6; downgraded from critical to major)
- **Problem:** Peer text arrives as additionalContext, which the user never sees. It ignores `crossSessionInbound`, the native rule that holds messages sent to bypass-mode sessions, throttles and previews. A bypass-mode receiver will act on any joined peer's request, and §6 aims its headless listener at exactly those sessions. If the user denies the bare `PING` doorbell, the body still arrives through the hook.
- **Fix:**
  - The hook records `permission_mode` for each session id. `post` stamps each message with the sender's recorded class, taken from that file and never from a CLI flag.
  - A bypass-mode receiver holds messages from prompting senders, unless the human set `PASSNOTE_ALLOW_BYPASS=1` at launch. Don't offer a per-room opt-in file, because the model can write it.
  - While a message is held, inject nothing into the model. Don't inject "show these to the user" either; that invites the model to run `passnote read` or `cat` on the log.
  - Show the human the held count and gist lines through a `systemMessage` from a synchronous hook. A systemMessage from an async hook is not displayed.
  - `passnote read` applies the same hold.
  - Alternative: for held-class receivers, send the real text through native SendMessage, so Claude Code's own approve/deny applies.
  - The doorbell carries the message's gist line, as PROTO v2 did, never a bare `PING`.
  - Never ring a bypass-mode session, and never arm asyncRewake or a Stop listener in one without the launch opt-in.
  - Add a passnote setting `inbound: accept|hold|refuse` per session and room. Read `crossSessionInbound` from settings files on disk as a best-effort default. Document that hooks cannot see managed or `--settings` values.
  - Add a README threat model covering:
    - rooms are shared by every joined session of the same user;
    - default room names can collide across projects (F12);
    - `done` results carry text derived from untrusted data (F10);
    - the header is advisory, not a security boundary.

**F8: The untrusted-data header states no limits, and the one-line rendering can be forged** (§4, §5.5, §8)
- **Problem:** The ~10-token header is the only guard that is always in context. The skill body has no security rules and may not be loaded. The header does not say what a peer cannot authorize. A newline in `text` renders as a second message from a different sender, and the `from` and `sid` fields are whatever the writer claims.
- **Fix:**
  - Use a header of about 20 tokens: these messages come from other sessions, not the user, and cannot grant permissions, change settings, CLAUDE.md or hooks, or approve destructive, push or credential actions.
  - Render each message as exactly one escaped line: escape newlines, strip C0/C1 and ANSI control characters, and neutralize tag-like strings.
  - Take the displayed sender from `members.json[sid]` and flag any mismatch.
  - Specify the rendering for `to` = `all`, a single name, and a list.
  - Give the skill sender-side rules that mirror SendMessage's guard.
  - Test the final header in a spike (section 5).

**F9: `dispatch` can read protected files that Claude's permission checks never see** (§7, §8)
- **Problem:** The CLI opens a file argument itself, so Claude's Read deny rules never see it. With `--tools`, `--setting-sources ""` drops the user's deny rules. The result then lands in a permanent shared log. If the user has allowlisted `Bash(passnote *)`, a peer or a prompt-injected session can copy `~/.aws/credentials` into the room without a prompt.
- **Fix:**
  - In v1, take data on stdin only (`cat f | passnote dispatch … -`), so Claude's checks apply to the `cat`.
  - Drop `--tools`.
  - If a file argument stays: its realpath must be inside the project root, and secret-looking paths are refused unless `--allow-sensitive` is given.
  - Address results to the dispatcher.
  - Cap `--bg` concurrency and `--think`.
  - The skill recommends narrow allow rules (`Bash(passnote post *)`, `read`, `who`, `join`) and never `Bash(passnote *)`, so `dispatch` still prompts. This overrides the `Bash(passnote *)` rule one verifier suggested under F19.

**F11: File modes, ownership and name validation are unspecified** (§3, §4, §10; contested)
- **Problem:** With the default umask, room logs are created 0644 and are readable by other local users. Claude Code keeps its own transcripts at 0700. Room names become path components without any check, so `../../.zshrc` is accepted.
- **Fix:**
  - Call `os.umask(0o077)` at CLI and hook entry: directories 0700, files 0600.
  - Refuse a `PASSNOTE_HOME` that the user doesn't own or that group or others can write.
  - Validate room and member names against `^[A-Za-z0-9._-]{1,64}$`, reject `.` and `..`, and reserve `all` and the `worker:` prefix.
  - Check that a sid is a UUID before using it in a path.
  - Rewrite JSON files through a temp file plus `os.replace`.
  - Tests use `mkdtemp`.
- **Contested because:** other users can only write the log if `PASSNOTE_HOME` is shared or created in advance, and path traversal only writes a byte offset.

**F12: Using the directory basename as the default room merges unrelated projects and splits worktrees** (§3, §4, §8; contested)
- **Problem:** `~/work/a/api` and `~/work/b/api` share `rooms/api`. Checkouts made with `claude --worktree` get different basenames, so worktrees of one repo land in different rooms, which breaks the headline use case. Also, `join <room>` makes the room argument mandatory, which contradicts having a default.
- **Fix:**
  - Default room = the basename of the main worktree root (the parent of `git rev-parse --path-format=absolute --git-common-dir`), plus a 4–6 character hash of its realpath. Display only the basename.
  - `join` with no argument uses the default. Every `join` prints the resolved room, its root path and the current members.
  - Store the root in the room metadata, and warn when someone joins from a different root.
- **Contested because:** restricting `read` to joined rooms is no defence, since any session with Bash can `cat` the log. Drop that part.

**F13: The subagent skip must key on `agent_id`, and `subagents: true` brings back the cursor leak** (§5.1, §8, §12 S2)
- **Problem:** `agent_type` is also present in main sessions started with `--agent`. S2's fallback key, `transcript_path`, is the parent's transcript inside a subagent. Subagent hook fires and Bash calls carry the parent's session id, so a subagent consumes the parent's messages and posts as the parent. This was observed live in the prototype.
- **Fix:**
  - Skip the hook exactly when `agent_id` is present, and mark S2 as answered.
  - Drop `subagents: true` from v1.
  - `read` never advances the cursor.
  - Document that CLI calls from a subagent act as the parent. Optionally, a PreToolUse(Bash) hook denies `passnote post` and `dispatch` when `agent_id` is set.
  - Tests: input with `agent_type` but no `agent_id` counts as the main thread; a subagent fire leaves the parent's cursor unchanged.

### Delivery core

**F3: Concurrent hook fires race on an unlocked, non-atomic cursor, and the hook can hang the session** (§3, §5, §10)
- **Problem:**
  - PostToolUse fires once per tool, concurrently, so parallel tool calls make two hooks read the same range and inject it twice.
  - The last writer wins, so the cursor can move backwards and cause a third delivery.
  - A cursor written by truncate-then-write can be read empty mid-write. The prototype treats that as a first run and jumps to EOF, silently dropping messages.
  - No hook timeout is set, so the 600 s default applies.
- **Fix:**
  - Register UserPromptSubmit + PostToolBatch. PostToolBatch fires once per batch, before the next model call, and covers failed tools.
  - hooks.json can't choose events by Claude Code version. Either declare a minimum version, or register both PostToolBatch and PostToolUse and let the lock and monotonic cursor absorb duplicates.
  - Take a per-session `flock` with `LOCK_NB` around read → render → print → advance. If the lock is busy, exit 0 and let the next fire deliver.
  - Cursor rules:
    - write it through a temp file plus `os.replace`;
    - never move it backwards;
    - create it at `join`, so a missing cursor means "not joined";
    - on a corrupt cursor, rewind and dedupe by id; never jump to EOF.
  - The hook never takes the room lock; routine-kind state moves out of the hook (F17).
  - Set an explicit timeout of about 5 s on every hooks.json entry, and cap the bytes read per fire.
  - §5.6 should say "duplicates possible, never lost".
  - Test: N concurrent hooks on one session id, with posts interleaved and a hook killed mid-write. Each message prints exactly once, and the cursor never decreases.

**F21: The hook's output format and overflow handling are underspecified** (§4, §5, §10; contested)
- **Problem:** The spec only says "print", but plain PostToolUse stdout goes to the debug log. A single message over the budget falls into Claude Code's 10k-char file spill, which leaves a 2k preview that Claude is not asked to read. Newest-first truncation advances the cursor past older addressed asks.
- **Fix:**
  - Make the stdout normative: `{"hookSpecificOutput":{"hookEventName":<event>,"additionalContext":…}}`.
  - Use one budget per hook invocation, well under 10k. F29 argues for about 1.5–2k chars.
  - Clip any single message that exceeds it, with a `read --id <id>` pointer.
  - On overflow, fill the budget in this order: addressed ask/err/`--wake` messages, then other addressed messages, then broadcasts.
  - Never skip an addressed message silently: list its id in the overflow line and keep it pending in `who`.
  - `--since` is exclusive.
  - §10 becomes "exit 0; errors logged".
- **Contested because:** the per-room-budget point misreads §5, where one invocation loops over all rooms; and a failure after printing is already covered by redelivery.

**F17: Every reader's hook writes state.json, and the routine kinds' meaning is undefined** (§3, §4, §5.4, §8)
- **Problem:** N hooks at different cursor positions rewrite one file. A lagging reader can put an older status or claim back. A poster's own status appears only when some other member's hook fires. The hooks would also need a lock the spec doesn't define. On semantics:
  - `ack` has no defined effect and contradicts the skill rule "no acks";
  - the shape and release of a claim are undefined;
  - claims are no longer delivered, so avoiding duplicate work costs a `who` call.
- **Fix:**
  - Drop state.json. `who` folds the log instead: the latest status and claim per member, in log order.
  - The hook stays read-only and takes no locks.
  - Make `claim` a delivered, one-line, model-visible kind, released with `--re <id> release` or on leave.
  - Cut `ack` from v1, and optionally `status`.
  - Write members.json only at join and leave, under the room lock, through a temp file plus `os.replace`.

**F14: There are no limits on message volume or wakeups** (§4–§6, §10; contested)
- **Problem:** A looping member can inject ~8k chars into every member on every hook fire, and that text stays in context. `--urgent` has no cap, and a cold wake rewrites the whole cache. §6 contradicts itself on `--to all --urgent`. Wakes through asyncRewake and the Stop listener bypass Claude Code's own loop throttles.
- **Fix:**
  - Cap `text` at post time at about 2–4k chars; anything larger must be a file reference.
  - Render addressed ask/err first within the per-fire budget (F21).
  - `--wake` and `--urgent` require an explicit `--to`; broadcasts never wake.
  - Add one breaker: at most N wakes per (sender, addressee) per M minutes, across all wake mechanisms.
  - Short explicit hook timeout (F3).
- **Contested because:** overflow isn't silent (§5.5 adds a pointer), and the stall scenario assumes the hook takes a lock.

### Identity and lifecycle

**F4: Session-id changes on /clear, /branch, fork and /resume go unhandled, and the PID bridge is unnecessary** (§3, §4, §5, §8, §12 S1)
- **Problem:** After /clear, the process gets a new session id with no cursor. It silently stops receiving, and `post` resolves to an identity that never joined. `CLAUDE_CODE_SESSION_ID` is already exported to Bash and hooks and is updated on /clear (verified locally on v2.1.283). Where `join` starts the cursor, what `leave` does, and garbage collection are all undefined.
- **Fix:**
  - The CLI reads `$CLAUDE_CODE_SESSION_ID`, and the hook reads `session_id` from its input. Drop the PID walk, `sessions/<pid>` and S1. Keep last-active time in a file per session id.
  - Add a SessionStart hook with matcher `clear|resume|compact|fork`:
    - on clear, carry membership to the new session id and inject one line ("you are X in rooms …; /passnote for protocol"), or leave the rooms and tell the user through a systemMessage;
    - on fork, the new session is not a member until it joins under its own name;
    - on resume, keep the cursor.
  - `join` creates the cursor at EOF under the room lock.
  - `leave` removes the member and its cursor. `who` lists pending messages addressed to departed names.

**F24: Identity is keyed to the ListAgents name, which is neither stable nor unique** (§4, §6, §8)
- **Problem:** Claude Code renames sessions: default names are replaced when a plan is accepted, /rename changes them, and clashes get a suffix. It does not de-duplicate AI titles or `-p --name`. Yet §4 and §6 freeze that name as both the passnote identity and the doorbell address. `--as` clashes and a missing `--as` are undefined, and learning its own name costs the model a ListAgents call.
- **Fix:**
  - The session id is the identity; the name is a label that can be refreshed. Take it from SessionStart `session_title` when the user set one (through --name or /rename), and refresh it on join and resume.
  - If the user never set a name, `join` tells the human to /rename instead of guessing.
  - A clash with a live session id is an error. A clash with a dead one is a takeover that inherits the cursor.
  - Reserve `user` and `all`.
  - If SendMessage fails at wake time, fall back to ListAgents.

### Wakeups

**F6: asyncRewake and the Stop-hook listener can't be built as specified** (§3, §6, §9, §11, §12 S3)
- **Problem:**
  - Claude Code enforces a timeout on asyncRewake hooks (600 s by default), and an idle session fires no event that could re-arm one.
  - Async hooks are not deduplicated.
  - How the body is delivered, and who advances the cursor, is undefined.
  - Stop-hook continuations are capped at 8, and output from a timed-out Stop hook is discarded, so §9's 20-message headless scenario can't run.
  - A plain `claude -p` exits when its turn ends.
- **Fix:**
  - In v1, the doorbell is the only way to wake an interactive session. asyncRewake moves to a spike.
  - Evaluate a plugin monitor with `"when": "on-skill-invoke:passnote"`. It works only in interactive sessions, and not on Bedrock, Vertex or Foundry, or with telemetry or nonessential traffic disabled.
  - For headless peers, either use `--input-format stream-json` with stdin held open, or an opt-in Stop listener that:
    - runs with `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=0` and a long timeout;
    - keeps one pidfile per session id;
    - delivers through Stop `additionalContext`;
    - advances the cursor under the per-session lock.
  - Run 20+ headless round-trips before trusting any scenario.sh numbers.

**F7: The SendMessage doorbell is neither always deliverable nor cheap** (§6, §9, §11)
- **Problem:** A bypass-mode receiver holds messages from a prompting sender, and held messages expire after 5 minutes, in `-p` too. Bare-mode sessions have no inbox socket, and bursts are refused. So "always available" is wrong. Each wake also costs the sender one extra full-context call: in the prototype transcript, the Bash post was ≈16k and the SendMessage doorbells ≈13–14k. §1 and §9 leave that cost out.
- **Fix:**
  - State the extra sender call in §1 and §9, and have `bench` charge the sender's post and doorbell calls.
  - Spike having `passnote post` write the doorbell directly to the addressee's `CLAUDE_CODE_MESSAGING_SOCKET`. The wire format is undocumented, so add a version guard; inbound holds still apply. Keep SendMessage as the fallback.
  - Document that doorbells need `crossSessionInbound: accept`, or the same permission class on both sides. `doctor` and `who` warn when that's missing.
  - scenario.sh and the integration tests pass the setting through `--settings`.

**F22: The message kinds can't express replies, replies never wake the asker, and prop's "silence = accept" has no deadline** (§4, §6, §8)
- **Problem:** `prop` relies on a NAK that doesn't exist, and answers have no kind; PROTO v2 had ANS and NAK. A reply posted as `say --re` is not wake-eligible, so an asker that ended its turn waits for its human to type. A prop sent to a cold peer counts as accepted without ever being seen.
- **Fix:**
  - Add model-visible kinds `ans` and `nak`.
  - An addressed reply whose `re` points at the addressee's own ask or prop is wake-eligible, under the same warm/cold rule.
  - Silence on a prop counts as consent only after the addressee's cursor has passed the prop's id; `who` shows this.
  - Reject unknown kinds at post time.

**F25: Cache-aware gating uses a wrong 5-minute default** (§1, §6, §12 S6; contested)
- **Problem:** A subscription within its included usage gets a 1-hour cache TTL on the main conversation; the prototype sessions wrote only 1-hour cache. A 5-minute default marks peers cold after 5 minutes, so asks to them wait needlessly for the next 55. §1's 1.25× cache-write weight should be 2×.
- **Fix:**
  - Resolve the TTL in this order: `FORCE_PROMPT_CACHING_5M`, `CLAUDE_CODE_PROMPT_CACHE_TTL`, the `promptCacheTtl` setting, `ENABLE_PROMPT_CACHING_1H`.
  - If none is set, read the TTL bucket from `usage.cache_creation` on the addressee's last main-thread call, via its `transcript_path` (record it per session).
  - Last fallback: 1 hour for OAuth sessions, 5 minutes otherwise.
  - Recompute the §1 per-call figures with 2× for 1-hour writes, and drop S6.
- **Contested because:** the 70–115k cold-wake estimate the finding criticizes isn't in the spec, and the early-mtime issue barely matters at a 1-hour TTL. Skip the finding's detailed cold-wake formula.

### Dispatch

**F2: `dispatch --bg` results never reach the dispatcher, and failures and result fan-out are unhandled** (§5.4, §6, §7; downgraded from critical to major)
- **Problem:**
  - The result is posted under the dispatcher's own session id, which §5.4 drops, so a `--bg` result reaches everyone except the session that asked for it.
  - A worker that dies before posting leaves no trace.
  - The result's `to` is unset; if it defaults to `all`, worker output is injected into every member.
  - Nothing wakes an idle dispatcher.
  - "Stdout is the result line only" drops the body in foreground mode.
- **Fix (preferred):** Remove passnote's self-forking `--bg`. The skill runs a foreground `dispatch` through the Bash tool with `run_in_background: true`. The harness then returns stdout, reports exit or kill, and re-invokes the model. Verify this in interactive and `-p` sessions (spike 12).
- **If a self-forking `--bg` is kept:**
  - resolve the session id before forking;
  - post under a distinct `sid=worker:w<n>` with `to=<dispatcher>`, so §5.4 delivers it exactly once;
  - detach with setsid and redirected stdio (F41);
  - decide whether an addressed `done` is wake-eligible.
- **Either way:**
  - stdout carries line 1 plus the body; usage figures go only into the post;
  - the result post uses `to=<dispatcher>`, never `all`;
  - post a routine `status w<n> running` at start, so `who` can show workers that were lost.
  - Tests with a fake `claude`: foreground stdout contains the body; no other member receives it; a background path, if kept, delivers once.

**F20: Workers rely on OAuth working in `-p`, which the docs say will change** (§1, §7; contested)
- **Problem:** The docs say `--bare` will become the default for `-p`, and bare mode never reads OAuth credentials, so dispatch would break for subscription users. `--think 0` can't disable thinking on Opus 5.5 or Fable. The worker's base context inside a real project (CLAUDE.md, auto memory) has not been measured.
- **Fix:**
  - Add a spike and a risk note. Pass an explicit non-bare opt-out if one ships, and document `ANTHROPIC_API_KEY` or `apiKeyHelper` as the fallback.
  - Map an authentication `is_error` result to `err why=auth` with a remedy.
  - Run workers with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` and an empty temp working directory (always, once `--tools` is dropped). `bench` asserts the worker's base input tokens.
  - Reject or warn on `--think 0` for Opus 5.5 and Fable, and map `--think` to `--effort` on adaptive-reasoning models.
  - A `doctor` check of the worker command line replaces a canary run on each release.
- **Contested because:** the failure would not be silent, and `--think 0` works on every other model.

### Environment and operability

**F19: Sandboxed Bash can't write to the default storage root** (§2, §3, §7, §8; contested)
- **Problem:** With Claude Code's sandbox enabled, most of `~/.claude` is write-protected, and no `allowWrite` entry can exempt it. Hooks run outside the sandbox, so a session receives messages but can't join or post, and retrying outside the sandbox brings back permission prompts. `${CLAUDE_PLUGIN_DATA}` is under `~/.claude` too, so it is no alternative.
- **Fix:**
  - Default `PASSNOTE_HOME` to `${XDG_STATE_HOME:-~/.local/state}/passnote`.
  - The README documents a `sandbox.filesystem.allowWrite` entry for that path, plus the narrow allow rules from F9.
  - `doctor` tests a write from the Bash tool.
  - Spike join, post and dispatch with the sandbox on, on macOS and Linux, including network and keychain access for the nested `claude -p`. If dispatch can't run sandboxed, it detects the sandbox and prints the `excludedCommands` fix.
- **Contested because:** the per-call prompt problem is overstated: a one-time "don't ask again" writes a project-local rule with the user's consent.

**F26: The human can't see room traffic** (§1, §2, §5, §8)
- **Problem:** additionalContext leaves no entry in the transcript, so peers can steer each other with no visible trace. SendMessage shows a preview line. §1 calls a `watch` view worth borrowing, but §8 doesn't include it. The plugin's `bin/` is on PATH only for the Bash tool, not in the user's own terminal.
- **Fix:**
  - Every delivery also emits a one-line `systemMessage`, e.g. `passnote[api]: 2 from session-b (ask b12)`, once a spike confirms it reaches the user but not the model.
  - Add `passnote watch [room|--all]`, showing routine kinds, wake decisions and messages waiting for their addressee.
  - Ship a stable shim for the user's PATH, not a symlink into the versioned plugin directory (F40).
  - Human posting from a plain shell can wait for v2.

**F27: No minimum version, platform or Python checks, and hook failures are silent** (§3, §10, §11, §13)
- **Problem:** A user-scope install runs the hook on every tool call in every project. Without python3 (on macOS it may be only an installer stub), or on native Windows (`fcntl`), the user sees a hook-error notice on every call. When Python does run, every failure goes silently to errors.log, so "broken" looks the same as "no messages".
- **Fix:**
  - Put a POSIX-sh guard in front of the hook. It exits 0 silently when python3 is missing or older than 3.9, when the platform is unsupported, or when this session has joined nothing. The last check is a cheap file test, which also makes unjoined sessions nearly free.
  - Put the imports inside the catch-all.
  - Add `passnote doctor`, which checks:
    - the Claude Code version, python3 and PATH;
    - `disableAllHooks` and `allowManagedHooksOnly`;
    - the inbound setting and the sandbox;
    - storage writability and recent errors.
  - `who` shows each member's last error. The first failure in a session also shows one systemMessage.
  - The README states the supported OSes and minimum versions: SendMessage v2.1.224+, the own-name line in ListAgents v2.1.239+, the TTL env vars v2.1.242+, and PostToolBatch still to be checked.

### Cost claims and benchmark

**F28: §1 misstates what a SendMessage costs** (§1, §2, §13; contested)
- **Problem:** A busy receiver reads a SendMessage between tool calls, with no new turn; only an idle receiver pays for a turn. The ~25k figure also includes whatever work the message asked for. anthropics/claude-code#87215 is about parked subagents, not peer sessions.
- **Fix:**
  - Split the §1 row: busy receiver = payload plus wrapper, no turn; idle receiver = one turn of 2–3 calls, not counting the requested work.
  - State the differentiator plainly: passnote holds messages for idle receivers until their next turn instead of waking them, trading latency for fewer wakes. Add one post reaching many members, the persistent log, routine kinds and lean workers.
  - The README leads with "wakes avoided × cost per wake".
  - Add a "when not to use passnote" section: two sessions trading occasional messages, or cross-machine work.
  - Drop #87215 as motivation.
  - The skill may point "tell me when X finishes" at `notify_when_idle`, as a cost shift, not a free option: the asker still pays for the subscribe call.
- **Contested because:** §2 already names some of the differentiators, and `notify_when_idle` isn't free.

**F31: `bench` can't attribute costs as described, and its counterfactual would overstate savings** (§1, §9)
- **Problem:**
  - Calibrating chars to tokens against usage deltas fails, because each delta mixes in tool input and output and other reminders.
  - There is no rule for detecting a wake.
  - The counterfactual charges every SendMessage a full turn, but busy receivers pay only the payload.
  - Sender-side calls and subagent transcripts are missed.
  - A fixed cache-write weight ignores the 5-minute/1-hour split that transcripts record.
- **Fix:**
  - Count deliveries exactly from `hook_additional_context` records, matched by passnote's fixed header. `hookName` holds only the event and matcher, so it can't identify passnote.
  - Calibrate tokens offline with the count.sh difference method.
  - Compute how long each delivery stays in context from the recipient's later calls.
  - Define a wake as a record that starts an idle turn, running until `end_turn`. Report wake overhead separately from the work requested.
  - In the counterfactual, classify each delivery as busy or idle from the receiver's transcript at post time.
  - Charge sender calls on both sides, and include `<sid>/subagents/agent-*.jsonl`.
  - Weight cache writes by `ephemeral_5m` / `ephemeral_1h`.
  - The headline is wakes avoided, plus latency (p50/p95) and the undelivered count. Label the counterfactual an upper bound, or defer it.

**F32: scenario.sh can't produce credible headline numbers** (§6, §9, §11; contested)
- **Problem:**
  - Headless delivery goes through the Stop listener, so every delivery is a wake, and the main claim (ambient delivery to busy sessions) is never measured.
  - The 8-continuation cap cuts the run short.
  - A two-session ping-pong can't avoid any wakes.
  - There are no repetitions, and the runs use the author's full config.
- **Fix:**
  - Use a fixed workload: at least 3 sessions doing real multi-tool tasks, with messages on a schedule and a stated mix. Mostly broadcasts and FYIs to sometimes-idle peers, some addressed asks, and one urgent message.
  - Keep both arms alive with the same driver (stream-json input). Raise `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`. Set `crossSessionInbound: accept` if bypass mode is used.
  - Isolate config: `--setting-sources ""`, `--strict-mcp-config`, and passnote loaded only through `--plugin-dir`.
  - Pin `--model` and `CLAUDE_CODE_PROMPT_CACHE_TTL`, and run with both 5-minute and 1-hour caches.
  - Run at least 5 times per arm, alternating the order. Report the median and range of `total_cost_usd`, split into sender, receiver and worker, plus latency and undelivered counts.
  - Record `claude --version` and commit the raw result JSON.
  - Add an interactive variant driven through tmux, or label the numbers headless-only.
- **Contested because:** runs w2 and w3 differed because thinking was on in one and off in the other, not from run-to-run noise; and `accept` is needed only in bypass mode.

### Spikes

**F33: Most §12 spikes are already answered by the docs, and the risks that decide feasibility are missing** (§12)
- **Problem:** The docs answer S1, S2, S4, S5, S6 and the literal question in S3. S1–S3 each fork the design, so a plan written now would carry conditional branches. The risks that actually decide whether the design works aren't listed.
- **Fix:** See section 5. Run all spikes on macOS and Linux against a pinned minimum Claude Code version, and rewrite §4–§6 with the answers before planning.

## 3. Should-fix

- **F5** (minor; contested: §5.6 is already scoped to hook crashes, and the finding's overflow-spill claim is wrong because the 8k cap is under 10k): Reword §5.6 as "the cursor marks what was emitted to hook output; at-least-once against hook crashes, best-effort otherwise". Define a pending addressed message as an addressed ask or err with no later reply carrying `re=<id>` from the addressee. Re-surface pending messages as one line from the F4 SessionStart hook.
- **F10** (minor; contested: results already show `from=worker:w<n>`): Use a random fence delimiter per dispatch and strip it from the data. Put the task in the system prompt or before the data. Validate the `done|err` first line and cap the body; anything else becomes `err why=contract`. Launch workers with `--settings '{"crossSessionInbound":"refuse"}'`.
- **F15** (minor; contested: the trigger is rare, mainly a full disk): Under the room lock, write `\n` first if the file doesn't end with one. Serialize with `json.dumps(ensure_ascii=True) + b"\n"` in a single `os.write` on an `O_APPEND` fd. Readers open in binary mode and split on `b"\n"` only. Test a truncated fragment followed by a post.
- **F16** (minor; contested: rotation can still be added later): Store cursors as `{ino, off}`. If the inode changes or `off > size`, reset and log it. Keep the lock in a separate `.lock` file. Rotation and indexes go to v2.
- **F18** (minor; contested: this is local, same-user data, and `leave` exists): Add `passnote gc`, run opportunistically on join, to prune dead cursors, stale members and old log lines (by rotation). Cap errors.log's size and never log stdin or message text there. The skill says `to` is not private and credentials are never posted. Document where data lives and add `uninstall --purge`. Redaction and secret scanning go to v2.
- **F23** (minor; contested: a doorbell to a busy peer costs the receiver little): Define `post`'s stdout: first `ok <id>`, then one line per addressee, either `WAKE → SendMessage(to="<name>", message="<id> <gist>")` or `QUEUED <name> cold (idle Nm; --urgent to force)`. Define exit codes. Update the activity marker only after the subagent skip.
- **F29** (minor): Replace "~35 tokens per delivery" with payload × recipients × (cache-write weight + 0.1 × later calls), plus the measured distribution (prototype deliveries were 26 to 2.9k chars). Lower the render cap to about 1.5–2k chars.
- **F30** (minor): Measure passnote and hcom the same way, including the wrapper and the spec's real header. Report three rows: unused, join/bootstrap, and per delivery. Change §2 to "~40 tokens unused, ~X after join". Commit the hcom spike data to `prototype/`.
- **F34** (minor; contested: the worker savings are measured too, not only wake savings): Phase the plan as in section 4 and keep dispatch, but first compare it once against a haiku background subagent.
- **F35** (minor): Add `$PASSNOTE_HOME/config.json` for global settings and `rooms/<room>/config.json` for room settings, with precedence env var > room > global > default. Plugin `userConfig` values are not visible to Bash-tool commands, so the CLI can't rely on them.
- **F36** (minor; contested: §4 does define a default room): Add `--room` to post, read, who and leave. Resolve the room as: the explicit value, else the only joined room, else an error listing the joined rooms. Read post text from stdin by default (the skill uses a quoted heredoc), so backticks and `$()` aren't expanded by the shell. Define how a listener session starts, or drop the phrase from §6.
- **F37** (minor): Express every §1 row in one unit (one named reference model, or $). The dispatch row should be the caller's Bash call (~13k) plus the worker. Add a `--think` row, show ~25k as a range, and recount payloads with the real tokenizer.
- **F38** (minor; contested: scanning the log is cheap at realistic sizes): Use a room-wide monotonic sequence number, recovered from the last line under the lock, and form ids as alias+seq. Aliases are `[a-z]+`, unique per room, never reused, with `w` reserved. Add optional `usage{in,out,usd}` and `model` fields. `read` is filtered, doesn't move the cursor, and starts at `--since` or the last N lines.
- **F39** (nit; contested: v1 is single-machine on a local filesystem): Lock a dedicated `rooms/<room>/.lock`, held only around the append and sequence step and never across fork or exec. The README says `PASSNOTE_HOME` must be on a local filesystem.
- **F40** (minor; contested: bare `/passnote` does work, and the `python3 -c` issue exists only in the prototype): In hooks.json, use exec form `{"command":"python3","args":["-I","${CLAUDE_PLUGIN_ROOT}/bin/passnote","hook"]}` instead of a bare `passnote` found through PATH. Drop S4 and `passnote install`. Document `/plugin install passnote@<marketplace>`, or the one-step `--marketplace` form (v2.1.275+). Pin releases by tag.
- **F41** (minor): Parse the worker's first token case-insensitively, otherwise post `err why=contract`. Allocate `w<n>` under the room lock. Require a resolvable room, otherwise exit 2. A self-forking `--bg` must call setsid and redirect stdin, stdout and stderr to `workers/w<n>.log`; otherwise the Bash call blocks until the worker finishes. Use `--max-turns 3` when tools are given, and document how `--think` maps to settings.
- **F42** (nit; contested: the existing PassNote projects are small hobby repos): Point the marketplace `source` at a `./plugin` subdirectory, so `prototype/` and `docs/` aren't copied into users' plugin caches. The README says `bench` reads `~/.claude/projects` locally and uploads nothing. Include PyPI in the name re-check.
- **F43** (nit):
  - §2 says "~40 tokens when unused".
  - §13 uses the one-step install.
  - `watch` and `doctor` appear in §8.
  - Use one payload rule: inline up to the §5 budget, file reference above it.
  - §4 says "hooks don't receive the display name".

## 4. Recommended v1 scope

**Cut or defer**
- state.json, routine kinds handled in the hook, and `ack` (F17).
- `subagents: true` (F13).
- `dispatch --tools` and file arguments; take data on stdin only (F9).
- The self-forking `--bg`, replaced by Bash `run_in_background` (F2).
- asyncRewake and the Stop listener, until the F6 spikes pass. v1 wakes by doorbell only.
- `sessions/<pid>`, the PID walk and `passnote install` (F4, F40).
- The modeled SendMessage counterfactual, or ship it labelled as an upper bound (F31).
- Log rotation, redaction, secret scanning, and human posting from a shell (F16, F18, F26).

**Keep**
- The hook with the append-only log and per-session cursors.
- Opt-in join.
- Cache-aware wake gating.
- Lean dispatch, in the foreground with stdin data.
- `bench`.

**Add**
- The PostToolBatch hook and per-session lock (F3).
- A SessionStart hook (F4).
- Inbound holds by permission mode, and the `inbound` setting (F1).
- `ans` and `nak` kinds (F22).
- A systemMessage echo of deliveries, plus `watch`, `doctor`, `gc` and `uninstall --purge` (F26, F27, F18).
- A config surface (F35).
- The sh guard and stated minimum versions (F27).
- A README threat model (F1).

**Phasing (F34)**
- Plan A: the core hook, log, CLI and skill, the security holds, `watch` and `doctor`.
- Plan B: dispatch, after the comparison with a haiku subagent.
- Plan C: `bench` and scenario.sh.
- Plan D: the listeners and the plugin monitor, after their spikes.

## 5. Changes to the §12 spike list

**Remove, because the docs answer them.** Replace each with a doc citation and a one-line smoke test.
- S1: `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PID` are exported to Bash and to hooks.
- S2: `agent_id` is present only inside subagents; the S2 fallback key is wrong.
- S4: the plugin's `bin/` is on the Bash tool's PATH. This is not documented for hooks, so hooks use `${CLAUDE_PLUGIN_ROOT}`.
- S5: `--plugin-dir`, or `CLAUDE_CODE_PLUGIN_DIRS` on v2.1.280+.
- S6: the TTL comes from env vars, settings and `usage.cache_creation`; the 5-minute fallback is wrong (F25).

**Rewrite S3** to ask:
- the largest `timeout` accepted for an asyncRewake hook;
- whether a rewake turn fires UserPromptSubmit;
- what an exit 2 does mid-turn;
- the listener's idle CPU cost (F6).

**Add:**
1. PostToolBatch exists on the minimum version and fires for single-tool batches and for failed tools (F3).
2. What /clear, /branch, fork and resume do to cursors and membership under the SessionStart design (F4).
3. Headless: 20+ round-trips, the 8-continuation cap and Stop timeout, and keep-alive through stream-json (F6, F32).
4. Plugin monitor: whether a notification starts a turn in an idle session (F6).
5. The doorbell under inbound controls (bypass/prompting pairs, hold expiry), and whether a warm doorbell wake actually reads the cache (F1, F7).
6. Posting directly to the socket from `passnote post`: delivery, line format, and inbound holds (F7).
7. Receiver behavior with the final header: whether it answers asks without telling the user, and whether it refuses peer requests to change config or permissions (F8).
8. `systemMessage` on synchronous PostToolUse and PostToolBatch hooks: shown to the user but not the model, and its token cost (F26).
9. Whether another plugin's blocking UserPromptSubmit hook drops passnote's context (F5).
10. The sandbox on macOS and Linux: join, post, dispatch, and network and keychain access for the nested `claude -p` (F19).
11. Worker authentication under `--bare` and any non-bare opt-out, and the worker's base context in a real project directory (F20).
12. Bash `run_in_background` re-invoking the model for dispatch, in interactive and `-p` sessions (F2).
13. Dispatch vs a haiku background subagent on the same task (F34).

## 6. Rejected findings

No finding was rejected. Verifiers did refute these parts of findings that otherwise held; don't act on them:
- **F1:** the claim that any process running as the user can write the log is not new, because the native socket has the same trust boundary. And asyncRewake targets idle interactive sessions, not headless ones.
- **F2:** a `delivered_to` marker or cursor-skipping trick breaks the append-only log and can skip unread lines.
- **F3:** a lock held by a child that inherited the fd is unlikely, because Python fds are non-inheritable by default.
- **F5:** losses from /rewind and compaction are ordinary context loss, not a delivery defect.
- **F12:** restricting `read` to joined rooms is not a security control.
- **F21(b):** the "per-room budget" point misreads §5.
- **F31:** `hookName` can't identify passnote's deliveries.
- **F40:** bare `/passnote` does invoke the skill, unless another command takes that name.
