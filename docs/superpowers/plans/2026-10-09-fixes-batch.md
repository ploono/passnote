# passnote fixes batch (#6 #7 #9 #31) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close four follow-up issues:
- #6: after `/clear`, a session's own earlier posts come back to it, and `read` hides its own earlier asks.
- #7: rare edge cases in the self-healing `/clear` carry-over.
- #9: a deleted and refilled room log. This batch documents it as a known limit.
- #31: about 20 small follow-ups from the field-feedback review.

**Architecture:** No new mechanism. Every fix stays inside the current design: one file log, one hook fire per turn, no daemon.
- #6 reuses `store.member_for_sid` (members.json `prev_sids`). The hook already reads members.json every fire.
- #7 tightens `hook._carry_over_from_clear` / `rooms.carry_over`:
  - serialize the carry under the session's delivery lock;
  - order the carry so the meta write is its commit point;
  - drop a stale by-pid record instead of paying `ps` on every fire;
  - finish a half-done carry on a second `/clear`;
  - emit the reminder line on the fire that heals.
- #31 adds bounds and caps that keep the hook's 8 KB and log-line invariants whatever the input, and makes two small product changes.

**Tech Stack:**
- Python 3.9+, stdlib only (`plugin/lib/passnote/`).
- `unittest` (`tests/`).
- POSIX sh guard (`plugin/hooks/guard.sh`, unchanged in this batch).

**Spec:** `docs/superpowers/specs/2026-09-26-passnote-design.md` (v2.2; this batch makes it v2.3), with the glossary in `CONTEXT.md`. Read each issue with `gh issue view N -R ploono/passnote --json title,body,comments`. Plain `gh issue view` prints nothing in this repo.

**Base:** `origin/main` at `1d06e75` ("Release 0.2.0 (#34)"), 622 tests (6 skipped), lint clean. `CHANGELOG.md` already has an empty `## [Unreleased]` above `## [0.2.0]`. This batch's entries go there.

## Triage

Each issue and each #31 bullet, with the outcome and the task that owns it. "Close" means a comment with the reason; the text is in **Issue comments** at the end, for the maintainer to post.

| Issue / bullet | Outcome | Reason (one line) | Task |
|---|---|---|---|
| #6 own earlier posts delivered back; `read` hides own earlier asks | include | Real bug with a known, small fix; also closes the spec's "asks before /clear aren't tracked" gap | 2 |
| #7.1 second `/clear` strands a half-carried session | include | SessionEnd's unjoined branch deletes a still-valid `prev_sid`; heal first instead | 4 |
| #7.2 heal can land in the wrong session (`/resume`) | defer | Spec §5 and A2 say resume runs in a new process (its own by-pid record). The in-process path is unconfirmed, and a `carry_to` gate would end today's heal when SessionStart never ran | — |
| #7.3 heal not serialized with the delivery lock | include | A late heal can overwrite the emit state a fire just saved, losing overflow | 4 |
| #7.4 carry only if the old meta's pid/token match | wontfix | Same-user processes are inside the threat model (§9). The meta pid mirror is legitimately skipped on LockBusy, so the check would fail real carries closed | — |
| #7.5 SessionStart retry rarely fires; emit carry after meta | include | `CARRY_RETRY_WITHIN` (2.0 s) equals the room-lock timeout, so a busy lock never retries. Moving the emit carry first makes the meta write the commit point | 3, 4 |
| #7.6 stale record costs `ps` every fire | include | Drop `prev_sid` when the start token is known and differs (or `prev_sid` is garbled) | 4 |
| #7.7 no reminder after a heal | include | Cheap; must go through `render.build`'s accounting | 4 |
| #7.8 carried refs not marked `redelivered` | include | One-line fix; keeps "re-rendered at most once" true across `/clear` | 3 |
| #9 deleted and refilled log loses early messages | defer (document now) | A real fix needs a log-identity field in room meta and in every cursor (a format change), minted at every log-creation site. Document the limit in spec §7 and README | 1 |
| #31 R1 `render.build` can exceed `MAX_CONTEXT_JSON` when even `MIN_CLIP` doesn't fit | include | Breaks the 8 KB invariant on forged input; a fixed-shape fallback line fixes it | 5 |
| #31 R2 256 KB log-line guarantee holds only at default `text_max_chars` | include | A config value must never make a post's line one the hook skips; clamp by the encoded size | 6 |
| #31 R3 forged huge `seq` makes long posts exit 2 with an opaque error | include (clearer error) | Bounding `last_seq` would number new posts below the forged line, and every reader skips out-of-order lines, so nobody would get them | 6 |
| #31 R4 pre-existing `files/` may be a symlink or wider mode | wontfix | Same posture as `log.jsonl`. `PASSNOTE_HOME` is already ownership- and mode-checked, and only same-user processes (inside the threat model) can plant it | — |
| #31 R5 forged future `ts` keeps a pending receipt forever | include | Add `ts <= now` to stored receipts and evidence (`track` already clamps log ts) | 5 |
| #31 R6 ageing evidence writes `emit.json` on an idle fire | include | Violates "no new writes on idle fires"; prune only in a save that happens anyway | 5 |
| #31 R7 `subscribe`/`unsubscribe` read the set outside the room lock | include | Lost update between two concurrent commands; move the read-modify-write under the lock | 7 |
| #31 R8 `who` prints a thread list unbounded | include | Forged members.json can blow up output that reaches the model; show 8, then `+N` | 7 |
| #31 P1 receipt lines have no `[room]` prefix | include | Small and clearly right: ids repeat across rooms. Kept ASCII by using only a valid-name label, and only for a session in more than one room | 8 |
| #31 P2 full-text files not sanitised (bidi, zero-width, tag) | wontfix | Those characters are legitimate content (ZWJ emoji, RTL text, flag tags), so stripping them makes the "full text" lossy. The file reaches the model through Read, like any other untrusted file | — |
| #31 P3 SKILL.md digest bullet doesn't mention `--wake` | close (already fixed) | The bullet already says "props, replies to yours, --wake lines, asks/errs/naks/ans to you stay whole" | — |
| #31 C1 private cross-module names; duplicated atomic write | include | Cheap; done first so later tasks use the final names | 1 |
| #31 C2 missing tests (7) | include | Pinning tests; each is placed in the task that owns its code | 1, 5, 6 |
| #31 C3 cosmetic: test nits, stray `build.py` line, "non-Latin" | include | Cheap. The CHANGELOG `[0.2.0]` wording is release history and is left as is | 1 |

## Global Constraints

**Code**
- Python 3.9 compatible. Every plugin module keeps `from __future__ import annotations`. No `match` statements and no runtime `X | Y` types.
- Standard library only.

**Hook behaviour**
- Near-zero cost when nothing is new. A fire with no new lines, no pending receipts and no pending carry reads no file it doesn't read today, and writes nothing.
  - Pruning stale emit-state entries alone is not a reason to write (Task 5).
  - An unjoined session with no by-pid `prev_sid` must still create no files (`test_delivery_for_an_unjoined_session_reads_only_the_by_pid_record`).
- Every context line (message, digest, fallback, receipt, reminder) is counted inside `render.build`'s `MAX_CONTEXT_JSON` (6,500) accounting. The whole hook output stays under 8 KB for any input, including forged log lines, a forged `members.json`, a forged room `meta.json` display, and a long `PASSNOTE_HOME`.
- One rendered message is exactly one line. The message id stays the first token of a delivered line (after an optional `[room] ` prefix).

**Safety**
- Holds and the secret guard stay fail-closed:
  - a held message is never injected, digested, shown by `read`/`who` inside a session, or reported seen;
  - the secret guard scans the full text of every post before anything is written.
- Forged fields fail open: deliver more, never hide a line, never crash.
  - The one exception this batch accepts is in **Decisions** (Task 2): a forged `prev_sids` entry can hide a departed sender's lines from one reader's hook, or show them in that reader's `read`. It is never a current member's lines. It is no new exposure: the same forger can append a raw log line with any `mode` stamp, and holds trust the stamp.
- A path is never taken from a log field.

**Docs**
- Wherever behaviour changes, update in the same task:
  - `README.md`;
  - `CHANGELOG.md` under the existing empty `## [Unreleased]`, with `### Fixed` / `### Changed` subsections created on first use. If a rebase ever leaves no `## [Unreleased]`, create it directly above the newest `## [x.y.z]` heading. Never edit the `[0.2.0]` section;
  - the design spec. Task 1 bumps the status to v2.3 and starts an amendment list; each task adds its bullet;
  - `CONTEXT.md` only for a new or changed term. None is expected.
- Site copy: check `site/index.html`, `site/llms.txt` and `tests/test_site.py` whenever a README sentence changes. README image alt texts must stay identical to the site's.
- `plugin/skills/passnote/SKILL.md` is 4,454 bytes at base. This batch expects **0 bytes** of change. If a task finds it must edit it, measure `wc -c` before and after, keep the delta under 100 bytes, and put it in the commit body.
- Vocabulary follows `CONTEXT.md`: "message", "post", "member", "thread", "seen receipt", "hold".

**Process**
- Test-first. For each behaviour step: write the failing test, run it and see it fail for the stated reason, implement, run it and see it pass.
  - Steps marked **(pinning)** add tests for behaviour that should already hold. They are expected to PASS when written. If one fails, that is a bug: fix it in the same task and say so in the commit body.
- Before every commit, the full suite and lint pass:
  - `python3 -m unittest discover -s tests -q`. 622 at base; the count only goes up, except that Task 4 rewrites one test in place;
  - `uvx ruff==0.16.10 check`.
- New env vars go into `tests/support.py` `CLEAR_ENV`. None is expected.
- PUBLIC repo: keep local paths, usernames and session ids out of code, docs, fixtures and commit messages. Tests build paths from `self.home` / `paths.home()`. The suite's stray `build.py` line contains a temp path, so never paste suite output into a commit or doc.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Never push or merge. The controller does that.
- Work only in the worktree for branch `fixes-batch`.

## Decisions

Real design choices, each with its reason. A reviewer should check the code against these.

1. **#6 trusts members.json `prev_sids`, as the issue proposes, for "is this my own line".** `render` and `fold` already trust it for display and claims, and it covers sessions cleared before this release.
   - The cost: a forged `prev_sids` entry in the reader's own entry, naming a sender who is no longer a member, hides that sender's lines from that reader. The same forged entry also makes that reader's hook track the departed sender's addressed asks/props for seen receipts. That adds no exposure: any member can already append a raw line carrying the reader's current sid, and a receipt still needs the addressee's delivery evidence. A current member is never affected, because `store.member_for_sid` prefers a member's own key.
   - A line from an earlier sid of mine is shown by `read` like one from my current sid, without the hold check. `who` already does this today. A hold check there would hide the session's own posts, which #6 says must not happen:
     - after a permission-class switch before the `/clear` (the stamp is the old class);
     - for a post stamped `"unknown"` (its fallback, the old session's recorded mode, is gone after the carry).
   - The check would protect nothing either: a forger of `prev_sids` can just as well append a raw line with any `mode` stamp.
2. **#7 item 2 is deferred, not built.** See Triage. Without it the guard and record format stay unchanged (no `carry_to` key).
3. **#7 item 4 is wontfix.** See Triage.
4. **The SessionStart retry runs only if a whole retry still fits a 4.5 s budget** (`CARRY_BUDGET`, of the 5 s hook timeout). Each attempt is a room-lock wait plus a meta-lock wait (`sessions.META_LOCK_TIMEOUT`, 1 s). The retry passes the remaining time as its room-lock timeout and runs only if that is at least 0.5 s.
5. **The fallback first line** (when even `MIN_CLIP` doesn't fit) is `<id> (too large to show in this turn; passnote read --id <id>)`, with the `[room] ` prefix in a multi-room fire.
   - It counts as emitted, so the cursor advances and a forged line can't block a room.
   - It is never delivery evidence for a seen receipt, because its text didn't reach the model.
6. **Log-line clamp:** `store.LOG_LINE_MAX = 64 * 1024` bytes per log line, measured on the encoded record, not by a per-character estimate. A text whose escaped prefix wouldn't fit keeps a shorter prefix and gets a full-text file. A post whose other fields alone (an unbounded `--re`) exceed the cap is refused with exit 2.
7. **A forged huge `seq` gets a clear error only.** `last_seq` is not bounded (see Triage R3).
8. **Receipt room labels** use the room's display name when it is a valid name (ASCII, as delivered lines show it), else the room id. They appear only when the session is in more than one room, so a single-room receipt line is unchanged. Amended in review: a label two joined rooms would share (alike displays, or a display equal to another room's id) falls back to the room ids, so one label never groups two rooms' ids.
9. **#9 is documented, not fixed.** See Triage.

## Review Focus

These are the inputs most likely to bite a real user while no task's main-path test covers them. Each line names the test that pins it.
1. **A forged `prev_sids` must never hide a current member's lines, and a session's own earlier posts must stay visible to it whatever its mode did since.**
   - Task 2: `test_a_prev_sid_naming_a_current_member_never_hides_its_lines`;
   - Task 2: `test_read_id_finds_my_earlier_ask_after_a_mode_switch` and `test_read_id_finds_my_earlier_ask_stamped_unknown`.
2. **A first line that can't fit even at `MIN_CLIP` must still make progress and keep the cap.** It must be emitted (the cursor advances), stay under 6,500 bytes, and never count as seen.
   - Task 5: `test_a_first_line_too_large_even_at_min_clip_falls_back_to_a_fixed_shape`;
   - Task 5: `test_a_fallback_line_is_not_delivery_evidence`.
3. **An idle fire writes nothing, even when old evidence ages out.** Task 5: `test_ageing_evidence_alone_never_writes_on_an_idle_fire`.
4. **A failed `ps` (start token `None`) must never drop a pending carry.** Task 4: `test_a_failed_start_token_lookup_keeps_the_prev_sid`.
5. **A large `PASSNOTE_TEXT_MAX_CHARS` with CJK text must still produce a log line the hook reads.** Task 6: `test_a_large_text_max_chars_still_writes_a_readable_log_line`.

## File map (after all tasks)

| File | Responsibility | Tasks |
|---|---|---|
| `plugin/lib/passnote/paths.py` | `valid_sid`, `atomic_write_text` (shared temp-file write) | 1 |
| `plugin/lib/passnote/render.py` | public `clip_field`; fallback first line; room-labelled receipt line | 1, 5, 8 |
| `plugin/lib/passnote/wake.py` | uses `render.clip_field` | 1 |
| `plugin/lib/passnote/store.py` | full-text write via `paths.atomic_write_text`; `LOG_LINE_MAX`, `fit_text`, `record_overhead`; clearer long-id error | 1, 6 |
| `plugin/lib/passnote/trust.py` | `own()`; `visibility(..., members=None)` | 2 |
| `plugin/lib/passnote/hook.py` | own-line check and receipt tracking; carry state machine; reminder cap and tail; ts checks; idle-fire save rule; receipt labels | 1, 2, 4, 5, 8 |
| `plugin/lib/passnote/rooms.py` | drop `_valid_sid`; `carry_over(lock_timeout)`, emit-before-meta, redelivered mark; `change_threads` | 1, 3, 7 |
| `plugin/lib/passnote/cli.py` | `read` own lines; post clamp; subscribe/unsubscribe under the lock; bounded thread lists | 2, 6, 7 |
| `tests/test_*.py` | as named per task | all |
| `README.md`, `CHANGELOG.md`, spec | docs | all |

---

### Task 1: Hygiene, wording and the #9 limit (closes #31 C1, C3, the doorbell test of C2; documents #9)

**Files:**
- Modify: `plugin/lib/passnote/paths.py`. Add `valid_sid` and `atomic_write_text`; `atomic_write_json` uses the shared helper.
- Modify: `plugin/lib/passnote/rooms.py`. Delete `_valid_sid` (line ~76); `join` uses `paths.valid_sid`.
- Modify: `plugin/lib/passnote/hook.py:353`. `rooms._valid_sid` becomes `paths.valid_sid`.
- Modify: `plugin/lib/passnote/render.py`. Rename `_clip_field` to `clip_field` at its definition and all 13 uses.
- Modify: `plugin/lib/passnote/wake.py:146-147`. Use `render.clip_field`.
- Modify: `plugin/lib/passnote/store.py`. Delete `_write_full_text`; `append_message` calls `paths.atomic_write_text`.
- Modify: `tests/test_wake.py`, `tests/test_paths.py`, `tests/test_site.py`, plus the blank-line fixes in `tests/test_cli.py`, `tests/test_cli_view.py`, `tests/test_hook_deliver.py`, `tests/test_packaging.py` and `tests/test_render.py`.
- Modify: `README.md:73` and spec §7 step 6 ("non-Latin" → "non-ASCII"); README "Data and uninstall" and spec §7 step 4 (#9); spec status block (v2.3).

**Interfaces:**
- Produces:
  - `paths.valid_sid(sid) -> bool`;
  - `paths.atomic_write_text(path: str, text: str) -> None`;
  - `render.clip_field(value, limit=FIELD_CLIP) -> str`.
  - Later tasks use these names; no task may use `render._clip_field` or `rooms._valid_sid`.

- [ ] **Step 1: Write the failing tests**

`tests/test_paths.py` (add to the existing `HomeCase`-based class that tests `atomic_write_json`, or a new `class AtomicWriteTest(HomeCase)`):

```python
    def test_valid_sid(self):
        self.assertTrue(paths.valid_sid("0f1e2d3c-4b5a-4968-8776-655443322110"))
        for bad in ("../x", "", None, 5, "S-A"):
            self.assertFalse(paths.valid_sid(bad), bad)

    def test_atomic_write_text_writes_0600_and_leaves_no_temp_file(self):
        path = os.path.join(paths.ensure_home(), "rooms", "r", "files", "a1.txt")
        paths.atomic_write_text(path, "漢\nline two")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "漢\nline two")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.listdir(os.path.dirname(path)), ["a1.txt"])
```

`tests/test_wake.py` (`DecideTest`, next to `test_doorbell_quotes_a_forged_id_safely`; #31 C2 "a long forged id in `doorbell_line`"):

```python
    def test_doorbell_clips_a_long_forged_id(self):
        line = wake.doorbell_line("bob", {"id": "a" * 500 + '"', "from": "alice", "text": "hi"})
        self.assertIn('message="' + "a" * render.FIELD_CLIP + "… from alice: ", line)
        self.assertEqual(line.count('"'), 4)
        self.assertLess(len(line), 200)
```

Add `render` to the test module's `from passnote import ...` line if it isn't there.

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.test_paths tests.test_wake -q`
Expected: `test_valid_sid` and `test_atomic_write_text...` FAIL with `AttributeError: module 'passnote.paths' has no attribute ...`. The doorbell test PASSES already (pinning). Keep it.

- [ ] **Step 3: Implement**

`plugin/lib/passnote/paths.py`: replace `atomic_write_json` with:

```python
def _atomic_write(path: str, write) -> None:
    """Write through a fresh 0600 temp file (O_EXCL) in the same 0700 directory, then os.replace,
    so a reader never sees a partial file. What tempfile.mkstemp does, without importing tempfile,
    which costs every hook several ms (P1)."""
    directory = makedirs(os.path.dirname(path))
    tmp = os.path.join(directory, f".tmp-{os.getpid()}-{os.urandom(6).hex()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            write(fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: str, obj) -> None:
    _atomic_write(path, lambda fh: json.dump(obj, fh, ensure_ascii=True, sort_keys=True))


def atomic_write_text(path: str, text: str) -> None:
    _atomic_write(path, lambda fh: fh.write(text))
```

And after `check_sid`:

```python
def valid_sid(sid) -> bool:
    try:
        check_sid(sid)
    except PassnoteError:
        return False
    return True
```

Then:
- `rooms.py`: delete `_valid_sid`; `if _valid_sid(other):` becomes `if paths.valid_sid(other):`.
- `hook.py:353`: `not rooms._valid_sid(target)` becomes `not paths.valid_sid(target)`.
- `render.py`: rename `def _clip_field` to `def clip_field` and update every use (`grep -n "_clip_field" plugin`).
- `wake.py`: `render._clip_field` becomes `render.clip_field`.
- `store.py`: delete `_write_full_text`; in `append_message`, `_write_full_text(written, full_text)` becomes `paths.atomic_write_text(written, full_text)`.
- `grep -rn "_write_full_text\|_clip_field\|_valid_sid" plugin tests` must print nothing (fix any test that patched the old names).

- [ ] **Step 4: Cosmetic fixes (#31 C3)**

1. Blank-line nits (8 at base):
   - run `uvx ruff==0.16.10 check --preview --select E301,E302,E303,E305,E306 --fix tests`;
   - check that `git diff --stat` touches only blank lines in `tests/test_cli.py`, `tests/test_cli_view.py`, `tests/test_hook_deliver.py`, `tests/test_packaging.py` and `tests/test_render.py`;
   - leave the unused tuple-unpacking names (`ctx, emitted, overflow = ...`). They are the suite's idiom, and `F841` is clean.
2. The stray `build.py` line: in `tests/test_site.py`, `BuildTest.test_build_refuses_a_folder_it_did_not_make`, capture stderr around `self.build.main([self.tmp])` and assert on it. Add `import contextlib` and `import io` to the test module if they're missing.

```python
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.build.main([self.tmp]), 2)
        self.assertIn("wasn't built by this script", err.getvalue())
```

   The suite output must no longer contain a `build.py:` line.
3. Wording:
   - `README.md:73` "non-Latin text may clip earlier" becomes "non-ASCII text may clip earlier";
   - spec §7 step 6 "Non-Latin text may clip earlier" becomes "Non-ASCII text may clip earlier";
   - leave `CHANGELOG.md` `[0.2.0]` as is (release history);
   - `git grep -n "non-Latin\|Non-Latin"` must then match only the CHANGELOG's `[0.2.0]` section.

- [ ] **Step 5: Document #9**

- Spec §7 step 4: append a sub-bullet:

```markdown
   - Known limit (#9): a log deleted by hand and recreated is still deduped by the old `seq`. If it grows past a member's cursor seq before that member's next fire, the new messages at or below that seq are never delivered to it. passnote never deletes a log. A fix needs a log identity in the room meta and in every cursor (a format change), so it is deferred.
```

- README, "Data and uninstall", after the `gc` paragraph:

```markdown
Don't delete or replace a room's `log.jsonl` by hand while sessions are joined to it: a member
that hasn't had a turn since can miss the first messages of the new log. To start a room afresh,
have every member run `passnote leave` first.
```

- Spec status block: change `**Status:** spec v2.2 (2026-10-09).` to `**Status:** spec v2.3 (<date of this batch>).` After the v2.2 bullet, add:

```markdown
- v2.3 (<date>) amends §5, §6, §7 and §10 from the fixes batch (#6, #7, #9, #31).
  - #9 (§7 step 4): a deleted and refilled log is a documented known limit.
```

- [ ] **Step 6: Run the suite and lint**

Run: `python3 -m unittest discover -s tests -q && uvx ruff==0.16.10 check`
Expected: OK with 625 tests (622 + 3), and no `build.py:` line in the output. Ruff: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add -A plugin tests README.md docs/superpowers/specs/2026-09-26-passnote-design.md
git commit -m "Hygiene: public clip_field/valid_sid, one atomic write, test nits; document #9

Closes the code-and-tests bullets of #31 that need no behaviour change
(private cross-module names, the duplicated temp-file write, a forged
long id in doorbell_line, blank-line nits, the stray build.py line,
non-ASCII wording) and documents #9 as a known limit.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

No CHANGELOG entry: nothing user-visible changes. (The README `#9` sentence is guidance, not a behaviour change.)

---

### Task 2: A session's own lines across /clear (closes #6)

**Decision 1 applies.**

**Files:**
- Modify: `plugin/lib/passnote/trust.py`. Add `own`; give `visibility` a `members=None` keyword; import `store`.
- Modify: `plugin/lib/passnote/hook.py`. `_Fire.verdict` passes the room's members; `_read_room` tracks receipts with `trust.own`.
- Modify: `plugin/lib/passnote/cli.py` (`cmd_read`). `_who_messages` already treats earlier sids as own and is left as is.
- Test: `tests/test_trust.py`, `tests/test_hook_deliver.py`, `tests/test_cli.py`.
- Docs: spec §7 step 5 and the seen-receipts known gap; §10 `read` row; CHANGELOG.

**Interfaces:**
- Consumes: `store.member_for_sid(members, sid) -> (member_sid, info) | None` (exists).
- Produces:
  - `trust.own(msg, sid, members=None) -> bool`;
  - `trust.visibility(msg, sid, me, receiver_mode, inbound, env, members=None)`. The new keyword is last; existing callers stay valid.

- [ ] **Step 1: Write the failing tests**

`tests/test_trust.py`. A new class; `trust.own` is pure:

```python
class OwnTest(unittest.TestCase):
    def test_own_matches_my_current_and_earlier_session_ids(self):
        members = {"S-NEW": {"name": "alice", "alias": "a", "prev_sids": ["S-OLD"]}, "S-B": {"name": "bob", "alias": "b"}}
        self.assertTrue(trust.own({"sid": "S-NEW"}, "S-NEW", members))
        self.assertTrue(trust.own({"sid": "S-OLD"}, "S-NEW", members))
        self.assertFalse(trust.own({"sid": "S-B"}, "S-NEW", members))
        self.assertFalse(trust.own({"sid": "S-OLD"}, "S-NEW", None))  # no members: only the current sid
        self.assertTrue(trust.own({"sid": "S-NEW"}, "S-NEW", None))

    def test_a_prev_sid_naming_a_current_member_is_not_mine(self):
        members = {"S-NEW": {"name": "alice", "alias": "a", "prev_sids": ["S-B"]}, "S-B": {"name": "bob", "alias": "b"}}
        self.assertFalse(trust.own({"sid": "S-B"}, "S-NEW", members))
```

`tests/test_hook_deliver.py`. A new class at the end (imports already include `rooms`, `sessions`, `store`, `new_sid`, `post`):

```python
class OwnAcrossClearTest(DeliverCase):
    def test_own_posts_from_before_clear_are_not_delivered_back(self):
        post(self.a, "r", "mine")  # past alice's cursor: she had no fire after posting
        new = new_sid()
        rooms.carry_over(self.a, new)
        self.assertIsNone(self.deliver(new))

    def test_an_ask_posted_just_before_clear_still_gets_its_seen_receipt(self):
        post(self.a, "r", "q", kind="ask", to=["bob"])
        new = new_sid()
        rooms.carry_over(self.a, new)
        self.deliver(new)     # its cursor passes a1: a pending receipt is recorded
        self.deliver(self.b)  # bob's turn delivers a1
        self.assertIn("passnote: seen by bob: a1", self.context(self.deliver(new)))

    def test_a_prev_sid_naming_a_current_member_never_hides_its_lines(self):
        members = store.load_members("r")
        members[self.a]["prev_sids"] = [self.b]
        store.save_members("r", members)
        post(self.b, "r", "from bob")
        self.assertIn("from bob", self.context(self.deliver(self.a)))
```

`tests/test_cli.py`. A new class (import `rooms`, `sessions`, `store`, `join`, `post`, `new_sid` from `support`/`passnote` as the module already does for others):

```python
class ReadAcrossClearTest(CliCase):
    def setUp(self):
        super().setUp()
        paths.ensure_home()
        join(self.a, "r", "alice")
        join(self.b, "r", "bob")
        post(self.a, "r", "split step 2?", kind="ask", to=["bob"])
        post(self.a, "r", "and step 3?", kind="ask", to=["bob"], mode="unknown")  # posted in the command that joined
        self.new = new_sid()
        rooms.carry_over(self.a, self.new)

    def test_read_shows_my_own_asks_from_before_clear(self):
        code, out, _ = self.run_cli(self.new, "read", "--last", "5")
        self.assertEqual(code, 0)
        self.assertIn("a1 alice→bob ask: split step 2?", out)

    def test_read_id_finds_my_own_ask_from_before_clear(self):
        code, out, _ = self.run_cli(self.new, "read", "--id", "a1")
        self.assertEqual(code, 0)
        self.assertEqual(out, "a1 alice→bob ask: split step 2?\n")

    def test_read_id_finds_my_earlier_ask_after_a_mode_switch(self):
        sessions.update_meta(self.new, lambda meta: meta.update(permission_mode="auto"))  # another class now
        code, out, _ = self.run_cli(self.new, "read", "--id", "a1")
        self.assertEqual((code, out), (0, "a1 alice→bob ask: split step 2?\n"))

    def test_read_id_finds_my_earlier_ask_stamped_unknown(self):
        code, out, _ = self.run_cli(self.new, "read", "--id", "a2")
        self.assertEqual((code, out), (0, "a2 alice→bob ask: and step 3?\n"))
```

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.test_trust tests.test_hook_deliver.OwnAcrossClearTest tests.test_cli.ReadAcrossClearTest -q`

Expected:
- `OwnTest` errors with `AttributeError: ... no attribute 'own'`;
- `test_own_posts_from_before_clear...` FAILS (a1 is delivered);
- `test_an_ask_posted_just_before_clear...` FAILS (no seen line);
- all four `read` tests FAIL (the earlier asks are not shown; `--id` exits 2).
- `test_a_prev_sid_naming_a_current_member...` PASSES (pinning).

- [ ] **Step 3: Implement**

`plugin/lib/passnote/trust.py`. Import line: `from . import config, sessions, store`. Add above `visibility`:

```python
def own(msg, sid, members=None) -> bool:
    """Whether msg is the session `sid`'s own: posted under `sid`, or under an earlier id of the member
    now listed as `sid` (its members.json prev_sids, kept across /clear). store.member_for_sid prefers
    a member's own key, so a prev_sid naming another current member never makes that member's lines
    mine. Without `members`, only the current sid counts."""
    if msg.get("sid") == sid:
        return True
    found = store.member_for_sid(members, msg.get("sid"))
    return found is not None and found[0] == sid
```

Change `visibility`'s signature and first test. Add to its docstring that own lines include those from before a /clear when `members` is given:

```python
def visibility(msg, sid, me, receiver_mode, inbound, env, members=None):
    ...
    if own(msg, sid, members) or msg.get("kind") == "status" or not addressed(msg, me):
        return "skip", None
```

`plugin/lib/passnote/hook.py`:
- `_Fire.verdict`: `members, _, inbound, me = self.room(room)` and `trust.visibility(msg, self.sid, me, self.receiver_mode, inbound, self.env, members)`.
- `_read_room`: replace `if msg["sid"] == fire.sid:` with `if trust.own(msg, fire.sid, fire.room(room)[0]):`. `fire.room` is cached per fire, and `verdict` reads it for this line anyway, so this adds no I/O.

`plugin/lib/passnote/cli.py`, `cmd_read`. Replace the loop body:

```python
        for msg in msgs:
            if trust.own(msg, sid, members):
                # visibility() skips the reader's own lines, including those from before a /clear (#6);
                # read shows them, as `who` does. No hold check: it would hide my own posts after a mode
                # switch, or one stamped "unknown" (its fallback, the old session's mode, is gone).
                visible.append(msg)
                continue
            verdict, reason = trust.visibility(msg, sid, me, meta.get("permission_mode"), inbound, env, members)
            if verdict == "hold" and reason == "receiver mode unknown":
                unrecorded += 1
            elif verdict == "hold":
                held += 1
            elif verdict == "deliver":
                visible.append(msg)
```

`_who_messages` is unchanged: it already keeps the session's own messages, including those from before a /clear.

- [ ] **Step 4: Run them and see them pass, then the suite**

Run: `python3 -m unittest discover -s tests -q && uvx ruff==0.16.10 check`
Expected: OK, 634 tests.

- [ ] **Step 5: Docs**

- Spec §7 step 5, first filter bullet: "lines with my own `sid`;" becomes "my own lines: my session id, or an earlier one from before a `/clear` (`prev_sids` in `members.json`; a `prev_sid` that names another current member counts as theirs);".
- Spec §7 seen receipts: replace "Known gap: asks posted before a `/clear` carry the old session id, so the new session doesn't track them. Pending receipts already recorded are carried over." with "Asks posted before a `/clear` are tracked like the session's own (`prev_sids`) when the new session's cursor passes them; pending receipts already recorded are carried over."
- Spec §10 `read` row: append "Shows your own messages, including those from before a `/clear`".
- Spec v2.3 list: `- #6 (§7 step 5, §10): a session's own lines include those from before a /clear (members.json prev_sids): never delivered back, shown by read, tracked for seen receipts.`
- CHANGELOG `## [Unreleased]` → `### Fixed`:
  `- After \`/clear\`, a session is no longer delivered its own earlier posts, \`passnote read\` shows its own earlier asks (\`read --id\` finds them), and an ask posted just before a \`/clear\` still gets its seen receipt (#6).`

- [ ] **Step 6: Commit**

```bash
git add -A plugin tests CHANGELOG.md docs/superpowers/specs/2026-09-26-passnote-design.md
git commit -m "A session's own lines include those from before /clear (#6)

trust.own resolves a line's sid through members.json prev_sids, so the
hook neither delivers a session's pre-/clear posts back to it nor misses
tracking a pre-/clear ask for its seen receipt, and read shows (and
--id finds) its own earlier asks, as who already did, also after a
permission-mode switch or an 'unknown' stamp.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: carry_over internals (closes #7 item 8 and the emit-order half of item 5)

**Files:**
- Modify: `plugin/lib/passnote/rooms.py` (`carry_over`, `_carry_emit`).
- Test: `tests/test_rooms.py` (`RoomsTest`).
- Docs: spec §5 clear row; CHANGELOG.

**Interfaces:**
- Produces:
  - `rooms.CARRY_LOCK_TIMEOUT = 2.0`;
  - `rooms.carry_over(old_sid, new_sid, lock_timeout=CARRY_LOCK_TIMEOUT) -> None`. Task 4 passes `lock_timeout` on its retry.

- [ ] **Step 1: Write the failing tests**

`tests/test_rooms.py`, in `RoomsTest`. Its `setUp` makes `self.a`, `self.b` and an ensured home; join explicitly. Import `mock` from `unittest` if the module doesn't already.

```python
    def test_carry_over_moves_the_emit_state_before_the_meta(self):
        join(self.a, "r", "alice")
        ref = {"room": "r", "off": 0, "len": 10, "id": "b1"}
        sessions.save_emit(self.a, [], [ref])
        new = new_sid()
        real = sessions.update_meta

        def busy_for_new(sid, fn):
            if sid == new:
                raise paths.LockBusy
            return real(sid, fn)

        with mock.patch.object(sessions, "update_meta", busy_for_new):
            with self.assertRaises(paths.LockBusy):
                rooms.carry_over(self.a, new)
        self.assertEqual(sessions.load_emit(new)["overflow"], [ref])  # moved before the commit point
        self.assertEqual(sessions.load_meta(new)["rooms"], [])
        rooms.carry_over(self.a, new)  # the retry finishes, without duplicating the ref
        self.assertEqual(sessions.load_emit(new)["overflow"], [ref])
        self.assertEqual(sessions.load_meta(new)["rooms"], ["r"])

    def test_carry_over_marks_unconfirmed_emissions_redelivered_and_leaves_overflow_unmarked(self):
        join(self.a, "r", "alice")
        emitted = {"room": "r", "off": 0, "len": 10, "id": "b1", "line": "b1 bob→all say: hi"}
        overflow = {"room": "r", "off": 20, "len": 10, "id": "b2"}
        sessions.save_emit(self.a, [emitted], [overflow])
        new = new_sid()
        rooms.carry_over(self.a, new)  # no transcript recorded: nothing confirms the emission
        self.assertEqual(sessions.load_emit(new)["overflow"],
                         [overflow, {"room": "r", "off": 0, "len": 10, "id": "b1", "redelivered": True}])

    def test_carry_over_passes_its_lock_timeout_to_the_room_lock(self):
        join(self.a, "r", "alice")
        real, timeouts = store.room_lock, []

        def spy(room, *args, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            return real(room, *args, **kwargs)

        with mock.patch.object(store, "room_lock", spy):
            rooms.carry_over(self.a, new_sid(), lock_timeout=0.7)
        self.assertEqual(timeouts, [0.7])
```

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.test_rooms -q`
Expected:
- the first FAILS (overflow is `[]` after the LockBusy);
- the second FAILS (the carried ref has no `redelivered`);
- the third errors with `TypeError: carry_over() got an unexpected keyword argument 'lock_timeout'`.

- [ ] **Step 3: Implement**

`plugin/lib/passnote/rooms.py`:

```python
# Seconds carry_over waits for each room lock. SessionStart's 5 s timeout must fit an attempt and a
# retry (hook.CARRY_BUDGET).
CARRY_LOCK_TIMEOUT = 2.0
```

In `carry_over`:
- the signature becomes `def carry_over(old_sid, new_sid, lock_timeout=CARRY_LOCK_TIMEOUT) -> None:`;
- the lock line becomes `with store.room_lock(room, timeout=lock_timeout):`;
- reorder the tail:

```python
    # The emit state before the meta (#7): once the new meta lists the rooms, the session counts as
    # joined and no fire heals it again, so everything must have moved by then. Re-running is
    # harmless: _carry_emit merges without duplicates.
    _carry_emit(old_sid, new_sid, old)
    sessions.update_meta(new_sid, inherit)
    shutil.rmtree(paths.session_dir(old_sid), ignore_errors=True)
```

In `_carry_emit`, mark only the unconfirmed emitted refs:

```python
    # Carried as overflow, to be rendered (and its line recorded) again, at most once more: marked
    # redelivered, as _take_from_last_fire marks its own (#7). Old overflow was never emitted: unmarked.
    unconfirmed = [dict({key: value for key, value in ref.items() if key != "line"}, redelivered=True)
                   for ref in (emitted if missing is None else missing)]
```

Update the `carry_over` docstring ("Safe to run again…") to say the emit state moves before the meta.

- [ ] **Step 4: Run the suite and lint**

Run: `python3 -m unittest discover -s tests -q && uvx ruff==0.16.10 check`
Expected: OK, 637 tests. If an existing carry test asserted an unmarked carried emission (for example `test_carry_over_carries_only_unconfirmed_emissions_checked_in_the_old_transcript`), update its expected refs to include `"redelivered": True` and say so in the commit body.

- [ ] **Step 5: Docs and commit**

- Spec §5, the `clear` row: append "The carry moves the emit state before the new meta (the meta write is its commit point); emissions the old transcript doesn't confirm come back marked redelivered, so they are rendered again at most once."
- Spec v2.3 list: `- #7 (§5, §7): /clear carry-over hardening` (Task 4 extends this bullet).
- CHANGELOG `### Fixed`: `- A \`/clear\` interrupted between moving membership and the session record no longer loses the messages waiting to be delivered, and a message re-sent after \`/clear\` is re-sent at most once (#7).`

```bash
git add -A plugin tests CHANGELOG.md docs/superpowers/specs/2026-09-26-passnote-design.md
git commit -m "carry_over: emit state before the meta, carried emissions marked (#7)

The new meta is the carry's commit point: once it lists the rooms no
fire heals again, so the emit state now moves first. Unconfirmed
emissions carried as overflow are marked redelivered, as the hook
marks its own. carry_over takes a lock_timeout for the retry in the
next commit.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The /clear carry state machine (closes #7 items 1, 3, 6, 7 and the retry half of 5)

**Decisions 2, 3 and 4 apply.** The by-pid record format and `guard.sh` are unchanged.

**Files:**
- Modify: `plugin/lib/passnote/hook.py`: `handle_deliver`, `handle_session_end`, `handle_session_start`, `_carry_with_retry`, `_carry_over_from_clear`, `_deliver_locked`, `_reminder`, and new `_drop_prev_sid`, `CARRY_BUDGET`, `CARRY_MIN_RETRY`, `REMINDER_MAX_JSON`, `_clock`. Remove `CARRY_RETRY_WITHIN`.
- Test: `tests/test_hook_lifecycle.py`.
- Docs: spec §5; CHANGELOG.

**Interfaces:**
- Consumes: `rooms.carry_over(old, new, lock_timeout=...)`, `rooms.CARRY_LOCK_TIMEOUT` (Task 3); `sessions.META_LOCK_TIMEOUT` (exists, 1.0).
- Produces:
  - `hook._carry_over_from_clear(sid, pid, lock_timeout=rooms.CARRY_LOCK_TIMEOUT) -> bool` (True if a carry ran);
  - `hook._deliver_locked(sid, meta, inp, event, env, reminder=None)`;
  - `hook.REMINDER_MAX_JSON = 1000`;
  - `hook._clock` (module-level `time.monotonic` alias; tests patch it).

**Lock order** (state it in the code comment): session `.lock` (non-blocking), then room locks, then the meta lock. That is the order `cmd_leave` already uses (session lock, then `rooms.leave` with room and meta locks), so no new inversion.

- [ ] **Step 1: Write the failing tests**

In `tests/test_hook_lifecycle.py`, **rewrite** `test_session_end_of_an_unjoined_session_clears_a_stale_prev_sid`, which encoded the bug (#7.1), as the first test below. Add the rest to `LifecycleTest`.

```python
    def test_a_second_clear_before_any_fire_still_carries(self):
        self.run_hook("SessionEnd", self.b, reason="clear")
        mid, last = new_sid(), new_sid()
        with mock.patch.object(store, "room_lock", side_effect=paths.LockBusy):
            self.assertIsNone(self.run_hook("SessionStart", mid, source="clear"))  # the carry fails
        self.run_hook("SessionEnd", mid, reason="clear")  # cleared again before any fire
        out = self.run_hook("SessionStart", last, source="clear")
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"],
                         "passnote: you are bob in rooms r; /passnote for the protocol")
        members = store.load_members("r")
        self.assertIn(last, members)
        self.assertNotIn(self.b, members)
        self.assertNotIn(mid, members)

    def test_session_end_keeps_the_prev_sid_when_its_heal_is_busy(self):
        self.run_hook("SessionEnd", self.b, reason="clear")
        mid, last = new_sid(), new_sid()
        with mock.patch.object(store, "room_lock", side_effect=paths.LockBusy):
            self.run_hook("SessionStart", mid, source="clear")
            self.run_hook("SessionEnd", mid, reason="clear")
        self.assertEqual(sessions.read_by_pid(self.pid)["prev_sid"], self.b)
        self.run_hook("SessionStart", last, source="clear")
        self.assertIn(last, store.load_members("r"))

    def _stranded(self):
        """bob cleared; SessionStart(clear) moved room r but not r2 (busy): a half-carried session."""
        join(self.b, "r2", "bob")
        self.run_hook("SessionEnd", self.b, reason="clear")
        new = new_sid()
        real = store.room_lock

        def flaky(room, *args, **kwargs):
            if room == "r2":
                raise paths.LockBusy
            return real(room, *args, **kwargs)

        with mock.patch.object(store, "room_lock", flaky):
            self.run_hook("SessionStart", new, source="clear")
        return new

    def test_a_heal_waits_for_the_delivery_lock(self):
        new = self._stranded()
        post(self.a, "r", "one")
        fire = lambda: hook.main("PostToolBatch", hook_input(new), self.env(new, CLAUDE_PID=self.pid))  # noqa: E731
        with sessions.session_lock(new):
            self.assertEqual(fire(), "")
        self.assertEqual(sessions.load_meta(new)["rooms"], [])
        self.assertIn("one", fire())

    def test_session_start_carry_waits_for_the_delivery_lock(self):
        self.run_hook("SessionEnd", self.b, reason="clear")
        new = new_sid()
        with sessions.session_lock(new):
            self.assertIsNone(self.run_hook("SessionStart", new, source="clear"))
        self.assertIn(self.b, store.load_members("r"))
        self.assertEqual(sessions.read_by_pid(self.pid)["prev_sid"], self.b)

    def _once_busy(self, timeouts):
        real = store.room_lock

        def once_busy(room, *args, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            if len(timeouts) == 1:
                raise paths.LockBusy
            return real(room, *args, **kwargs)

        return once_busy

    def test_a_retry_runs_when_a_whole_retry_still_fits_the_budget(self):
        self.run_hook("SessionEnd", self.b, reason="clear")
        timeouts, clock = [], iter([0.0, 2.1])
        new = new_sid()
        with mock.patch.object(store, "room_lock", self._once_busy(timeouts)), \
                mock.patch.object(hook, "_clock", lambda: next(clock)):
            out = self.run_hook("SessionStart", new, source="clear")
        self.assertIn("you are bob", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(timeouts[0], rooms.CARRY_LOCK_TIMEOUT)
        self.assertAlmostEqual(timeouts[1], hook.CARRY_BUDGET - 2.1 - sessions.META_LOCK_TIMEOUT)

    def test_no_retry_once_the_budget_is_spent(self):
        self.run_hook("SessionEnd", self.b, reason="clear")
        timeouts, clock = [], iter([0.0, 3.1])
        new = new_sid()
        with mock.patch.object(store, "room_lock", self._once_busy(timeouts)), \
                mock.patch.object(hook, "_clock", lambda: next(clock)):
            self.assertIsNone(self.run_hook("SessionStart", new, source="clear"))
        self.assertEqual(len(timeouts), 1)
        self.assertIn(self.b, store.load_members("r"))

    def test_a_mismatched_start_token_drops_the_prev_sid(self):
        self.run_hook("SessionEnd", self.b, reason="clear")
        record = sessions.read_by_pid(self.pid)
        record["pid_started_at"] = "Thu Jan  1 00:00:00 1970"
        paths.atomic_write_json(sessions.by_pid_path(self.pid), record)
        new = new_sid()
        hook.main("PostToolBatch", hook_input(new), self.env(new, CLAUDE_PID=self.pid))
        self.assertNotIn("prev_sid", sessions.read_by_pid(self.pid))
        self.assertIn(self.b, store.load_members("r"))

    def test_a_failed_start_token_lookup_keeps_the_prev_sid(self):
        self.run_hook("SessionEnd", self.b, reason="clear")
        new = new_sid()
        with mock.patch.object(sessions, "pid_started_at", return_value=None):
            hook.main("PostToolBatch", hook_input(new), self.env(new, CLAUDE_PID=self.pid))
        self.assertEqual(sessions.read_by_pid(self.pid)["prev_sid"], self.b)

    def test_a_garbled_prev_sid_is_dropped_without_an_error(self):
        paths.atomic_write_json(sessions.by_pid_path(self.pid),
                                {"sid": self.b, "pid_started_at": sessions.pid_started_at(self.pid), "prev_sid": "../x"})
        new = new_sid()
        self.assertEqual(hook.main("PostToolBatch", hook_input(new), self.env(new, CLAUDE_PID=self.pid)), "")
        self.assertNotIn("prev_sid", sessions.read_by_pid(self.pid))
        self.assertFalse(os.path.exists(os.path.join(self.home, "errors.log")))

    def test_the_healing_fire_says_who_you_are(self):
        new = self._stranded()
        post(self.a, "r", "one")
        out = json.loads(hook.main("PostToolBatch", hook_input(new), self.env(new, CLAUDE_PID=self.pid)))
        lines = out["hookSpecificOutput"]["additionalContext"].split("\n")
        self.assertIn("passnote: you are bob in rooms r, r2; /passnote for the protocol", lines)
        self.assertTrue(any(line.endswith("say: one") for line in lines))
        # The next fire doesn't repeat it.
        post(self.a, "r", "two")
        again = hook.main("PostToolBatch", hook_input(new), self.env(new, CLAUDE_PID=self.pid))
        self.assertNotIn("you are bob", again)

    def test_the_reminder_has_a_fixed_shape_when_it_would_be_too_large(self):
        for i in range(5):
            room = f"big{i}"
            join(self.b, room, "bob")
            meta = store.load_meta(room)
            meta["display"] = "\U0001F600" * 64
            store.save_meta(room, meta)
        line = hook._reminder(self.b, sessions.load_meta(self.b))
        self.assertLessEqual(len(json.dumps(line)) - 2, hook.REMINDER_MAX_JSON)
        self.assertEqual(line, "passnote: you are a member of 6 room(s); passnote rooms lists them; "
                               "/passnote for the protocol")
```

The existing tests that must keep passing unchanged:
- `test_a_failed_carry_over_is_finished_by_the_next_delivery_fire`;
- `test_a_busy_lock_is_retried_once_inside_session_start`;
- `test_delivery_for_an_unjoined_session_reads_only_the_by_pid_record`;
- `test_pid_reuse_guard_blocks_carry_over`;
- `tests/test_packaging.py` `test_delivery_finishes_an_unfinished_clear_carry`.

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.test_hook_lifecycle -q`

Expected:
- `test_a_second_clear...` FAILS (`out` is None);
- `test_session_end_keeps...` FAILS with a `KeyError` on `prev_sid`;
- both lock tests FAIL (the carry runs despite the held lock);
- both retry tests error (`hook` has no `_clock`);
- the mismatched-token and garbled-sid tests FAIL (`prev_sid` stays; the garbled one also logs an error);
- the healing-fire test FAILS (no reminder line);
- the reminder-cap test FAILS (the line is about 2.4 KB of JSON);
- `test_a_failed_start_token_lookup_keeps_the_prev_sid` PASSES (pinning).

- [ ] **Step 3: Implement**

`plugin/lib/passnote/hook.py`. Replace `CARRY_RETRY_WITHIN` and its block with:

```python
# Of SessionStart's 5 s hook timeout, the most the /clear carry may use (#7). A retry after a busy lock
# runs only if a whole one still fits: a room-lock wait (the time left, at least CARRY_MIN_RETRY) plus
# a meta-lock wait (sessions.META_LOCK_TIMEOUT).
CARRY_BUDGET = 4.5
CARRY_MIN_RETRY = 0.5
_clock = time.monotonic  # module-level, so tests can drive the retry budget


def _carry_with_retry(sid, pid) -> bool:
    """Carry over, and once more if a lock was busy and a whole retry still fits CARRY_BUDGET. A
    failure past that leaves the by-pid record as it is: the next delivery fire finishes the carry."""
    started = _clock()
    try:
        return _carry_over_from_clear(sid, pid)
    except paths.LockBusy:
        left = CARRY_BUDGET - (_clock() - started) - sessions.META_LOCK_TIMEOUT
        if left < CARRY_MIN_RETRY:
            raise
        return _carry_over_from_clear(sid, pid, lock_timeout=min(left, rooms.CARRY_LOCK_TIMEOUT))


def _drop_prev_sid(pid, record) -> None:
    """Rewrite the by-pid record without prev_sid: guard.sh then stops sending this process's fires
    to Python for a carry that can never run."""
    paths.atomic_write_json(sessions.by_pid_path(pid), {k: v for k, v in record.items() if k != "prev_sid"})


def _carry_over_from_clear(sid, pid, lock_timeout=rooms.CARRY_LOCK_TIMEOUT) -> bool:
    """Finish a /clear carry-over to `sid`, if the by-pid record SessionEnd(clear) left names an old
    session. It counts only for the same process (a reused pid has another start token) and only
    until record_pid rewrites the record without prev_sid. True if a carry-over ran.

    - A prev_sid that isn't a session id, or a record whose start token is known and differs (another
      process got this pid), is dropped, so no later fire pays for it again (#7). A token that can't be
      read (`ps` failed) keeps the record: try again next fire.
    - The carry runs under `sid`'s delivery lock, non-blocking (#7): no fire of this session can save
      its emit state while the old one moves in. Lock order: session .lock, room locks, meta lock (as
      in `passnote leave`). LockBusy propagates.

    For a session with no rooms and no pending carry this costs one small file read."""
    if pid is None:
        return False
    record = sessions.read_by_pid(pid)
    prev = record.get("prev_sid") if record else None
    if not isinstance(prev, str):
        return False
    if not paths.valid_sid(prev):
        _drop_prev_sid(pid, record)
        return False
    started = sessions.pid_started_at(pid)
    if started is None:
        return False
    if record.get("pid_started_at") != started:
        _drop_prev_sid(pid, record)
        return False
    prev = paths.check_sid(prev)
    if prev == sid:
        return False
    with sessions.session_lock(sid):
        rooms.carry_over(prev, sid, lock_timeout=lock_timeout)
        sessions.record_pid(pid, sid)
    return True
```

`handle_deliver`: heal, then remind on the healing fire:

```python
    meta = sessions.load_meta(sid)
    healed = False
    if not meta["rooms"]:
        # Maybe a /clear whose carry-over didn't finish (SessionStart fires once): finish it now.
        try:
            healed = _carry_over_from_clear(sid, sessions.parse_pid(env.get("CLAUDE_PID")))
        except paths.LockBusy:
            return None
        meta = sessions.load_meta(sid)
        if not healed or not meta["rooms"]:
            return None
    sessions.touch_active(sid, now)
    try:
        meta = _refresh_meta(sid, meta, inp, event, env)
        with sessions.session_lock(sid):
            # The fire that healed a carry also says who the session is, as SessionStart would have (#7).
            return _deliver_locked(sid, meta, inp, event, env, reminder=_reminder(sid, meta) if healed else None)
    except paths.LockBusy:
        return None
```

The heal releases the session lock before the delivery takes it again. A concurrent fire that wins the lock in between delivers without the reminder, and the reminder is then lost. That is accepted for this best-effort line (#7.7).

`_deliver_locked(sid, meta, inp, event, env, reminder=None)`: the tail line becomes:

```python
    # The reminder (a healing fire only) and the receipt line end the context, inside build's caps.
    tail = "\n".join(part for part in (reminder, render.receipt_line(pairs)) if part) or None
```

`handle_session_end`. Replace the unjoined branch:

```python
    if not sessions.load_meta(sid)["rooms"]:
        # Unjoined. If a /clear's carry to this session never finished, it is cleared again before any
        # fire (#7): finish the carry now, so the membership moves on with this /clear instead of
        # being stranded with the old session.
        try:
            _carry_over_from_clear(sid, pid)
        except paths.LockBusy:
            return None  # the record keeps prev_sid: the next SessionStart(clear) carries from it
        if not sessions.load_meta(sid)["rooms"]:
            record = sessions.read_by_pid(pid)
            if record and record.get("prev_sid") == sid:
                _drop_prev_sid(pid, record)  # never a carry source while it has no rooms
            return None
    try:
        sessions.record_pid(pid, sid, prev_sid=sid)
    except paths.LockBusy:
        pass  # the pid record is written first; only its mirror into the meta was skipped
    return None
```

`handle_session_start`: unchanged except that `_carry_with_retry` now returns a bool (ignored there). Its LockBusy, from the session lock or a room lock, is still caught by the existing `except paths.LockBusy: return None`.

`_reminder`: cap the line:

```python
# The SessionStart / healing-fire reminder, as JSON: forged room displays or names (each up to 40
# characters, any of which can cost 12 bytes escaped) must not push the hook output past 8 KB.
REMINDER_MAX_JSON = 1000
```

and before `return line`:

```python
    if len(json.dumps(line, ensure_ascii=True)) - 2 > REMINDER_MAX_JSON:
        line = (f"passnote: you are a member of {len(meta['rooms'])} room(s); passnote rooms lists them; "
                "/passnote for the protocol")
```

- [ ] **Step 4: Run the suite and lint**

Run: `python3 -m unittest discover -s tests -q && uvx ruff==0.16.10 check`
Expected: OK, 647 tests (637 + 10 new; the rewritten test replaces the old one).

- [ ] **Step 5: Docs and commit**

- Spec §5, the `clear` row, append:
  - The carry runs under the new session's delivery `.lock`.
  - SessionStart retries once after a busy lock if a whole retry fits 4.5 s of its 5 s timeout.
  - A by-pid record whose start token is known and differs, or whose `prev_sid` is not a session id, is dropped.
  - A SessionEnd(clear) of a session whose own carry never finished finishes it first, so the membership moves on.
  - The delivery fire that finishes a carry also injects the "you are <name>…" line.
- Spec §5, after "After clear, resume and compact, the hook injects one line…": add "The line is at most 1,000 bytes as JSON; past that it is `passnote: you are a member of N room(s); passnote rooms lists them; /passnote for the protocol`."
- Spec v2.3 list: extend the #7 bullet with "(serialized under the session lock, a time-budgeted retry, stale records dropped, a second /clear finishes the first, a reminder after a heal; items 2 and 4 deferred/wontfix)".
- CHANGELOG `### Fixed`: `- A second \`/clear\` before the first one's carry-over finished no longer drops the session's rooms; a carry no longer races a delivery; a stale process record no longer runs \`ps\` on every turn; the turn that finishes a delayed carry says who you are (#7).`

```bash
git add -A plugin tests CHANGELOG.md docs/superpowers/specs/2026-09-26-passnote-design.md
git commit -m "/clear carry: serialized, budgeted retry, stale records dropped (#7)

- SessionEnd(clear) of a half-carried session finishes the carry
  instead of dropping prev_sid (item 1).
- The carry runs under the new session's delivery lock (item 3).
- SessionStart retries when a whole retry fits 4.5 s (item 5).
- A record with another process's start token, or a garbled prev_sid,
  is dropped; a failed ps keeps it (item 6).
- The healing fire injects the reminder line, capped like all context
  (item 7); the reminder has a fixed shape past 1,000 bytes.

Items 2 and 4 are deferred / wontfix; see the plan's triage.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Render and hook robustness (closes #31 R1, R5, R6 and four C2 tests)

**Decision 5 applies.**

**Files:**
- Modify: `plugin/lib/passnote/render.py` (`build`, new `_fallback_line`).
- Modify: `plugin/lib/passnote/hook.py` (`_valid_receipt`, `_Fire.__init__`, `_delivered`, `_deliver_locked`).
- Test: `tests/test_render.py` (`BuildTest`), `tests/test_hook_deliver.py` (`ReceiptTest`, `AddressedClipTest`, `DigestDeliverTest`, plus a new `EmitStateTest(DeliverCase)`).
- Docs: spec §7 step 6/7 and seen receipts; CHANGELOG.

**Interfaces:**
- Produces:
  - `render.build` emitted entries may carry `"fallback": True`;
  - `hook._delivered(previous, emitted, now) -> (list, bool)`: the evidence list, and whether new evidence was added;
  - `hook._valid_receipt(entry, rooms_joined, now) -> bool`.

- [ ] **Step 1: Write the failing tests**

`tests/test_render.py`, `BuildTest`:

```python
    def test_a_first_line_too_large_even_at_min_clip_falls_back_to_a_fixed_shape(self):
        wide = "\U0001F600" * 64
        m = msg(id=wide, to=[wide] * 4, kind=wide, re=wide, text="x" * 2000)
        items = [dict(item(m, room="r1"), display=wide), item(msg(id="b2", seq=2), room="r2")]
        ctx, emitted, _ = render.build(items, "bob", 2000, 600)
        self.assertLessEqual(len(json.dumps(ctx)) - 2, render.MAX_CONTEXT_JSON)
        self.assertTrue(emitted[0].get("fallback"))
        self.assertIn("(too large to show in this turn; passnote read --id ", emitted[0]["line"])
        self.assertTrue(emitted[0]["line"].startswith("["))  # multi-room: the room prefix stays
        self.assertEqual(len(emitted[0]["line"].splitlines()), 1)
        self.assertFalse(any(it.get("fallback") for it in emitted[1:]))

    def test_the_fallback_line_is_bounded_whatever_the_id(self):
        wide = "\U0001F600" * 5000
        line = render._fallback_line(dict(item(msg(id=wide)), display=wide), True)
        self.assertLess(len(json.dumps(line)), 2500)

    def test_the_overflow_line_still_names_an_id_after_the_first_line_shrinks(self):  # (pinning, #31 C2)
        items = [dict(item(msg(id="a1", seq=1, to=["bob"], kind="ask", text="\U0001F600" * 1500)), clip=1500),
                 item(msg(id="a2", seq=2, text="\U0001F600" * 100))]
        ctx, emitted, overflow = render.build(items, "bob", 2000, 600)
        self.assertEqual([it["msg"]["id"] for it in overflow], ["a2"])
        self.assertIn("… (+", emitted[0]["line"])  # shrunk
        self.assertIn("1 not shown yet: a2", ctx)
        self.assertLessEqual(len(json.dumps(ctx)) - 2, render.MAX_CONTEXT_JSON)
```

`tests/test_hook_deliver.py`, a new class:

```python
class EmitStateTest(DeliverCase):
    def test_a_fallback_line_is_not_delivery_evidence(self):
        it = {"room": "r", "msg": {"id": "a1", "seq": 1, "kind": "ask", "to": ["bob"]}, "me": "bob", "fallback": True}
        self.assertEqual(hook._delivered([], [it], time.time()), ([], False))

    def test_ageing_evidence_alone_never_writes_on_an_idle_fire(self):
        old = {"room": "r", "id": "x1", "seq": 1, "ts": time.time() - 2 * 86400}
        sessions.save_emit(self.b, [], [], delivered=[old])
        with mock.patch.object(sessions, "save_emit") as save:
            self.assertIsNone(self.deliver(self.b))
        save.assert_not_called()
        self.assertEqual(sessions.load_emit(self.b)["delivered"], [old])
        post(self.a, "r", "hi")
        self.deliver(self.b)  # a save that happens anyway prunes it
        self.assertEqual(sessions.load_emit(self.b)["delivered"], [])

    def test_evidence_with_a_future_ts_is_pruned_in_the_next_save(self):
        future = {"room": "r", "id": "x1", "seq": 1, "ts": time.time() + 10 * 86400}
        sessions.save_emit(self.b, [], [], delivered=[future])
        post(self.a, "r", "q", kind="ask", to=["bob"])
        self.deliver(self.b)
        self.assertEqual([entry["id"] for entry in sessions.load_emit(self.b)["delivered"]], ["a1"])
```

`ReceiptTest`:

```python
    def test_a_pending_receipt_with_a_future_ts_is_dropped(self):
        entry = {"room": "r", "id": "a1", "seq": 1, "ts": time.time() + 10 * 86400, "mode": "default", "to": ["bob"]}
        sessions.save_emit(self.a, [], [], receipts=[entry])
        self.deliver(self.a)
        self.assertEqual(sessions.load_emit(self.a)["receipts"], [])
```

The following are all **(pinning, #31 C2)**. `AddressedClipTest`:

```python
    def test_two_addressed_posts_with_the_larger_clip_overflow_then_arrive(self):
        post(self.a, "r", "x" * 1500, kind="ask", to=["bob"])
        post(self.a, "r", "y" * 1500, kind="ask", to=["bob"])
        first = self.context(self.deliver(self.b))
        self.assertIn("x" * 1500, first)
        self.assertIn("1 not shown yet: a2", first)
        self.assertIn("y" * 1500, self.context(self.deliver(self.b)))
```

`DigestDeliverTest` (its `setUp` turns digest on for bob in `r`):

```python
    def test_many_64_character_thread_names_stay_under_8_kb(self):
        join(self.a, "r2", "alice")
        join(self.b, "r2", "bob")
        rooms.set_prefs(self.b, "r2", digest=True)
        for room in ("r", "r2"):
            for i in range(12):
                post(self.a, room, "\U0001F600" * 300, thread="t" * 60 + f"{i:04d}")
        raw = hook.main("PostToolBatch", hook_input(self.b), self.env(self.b))
        self.assertLessEqual(len(raw.encode()), 8192)
        ctx = self.context(json.loads(raw))
        self.assertLessEqual(len(json.dumps(ctx)) - 2, render.MAX_CONTEXT_JSON)
        self.assertLessEqual(sum(1 for line in ctx.split("\n") if " new (" in line), render.DIGEST_MAX_LINES)

    def test_wholeness_uses_my_alias_in_each_room(self):
        bert = new_sid()
        join(bert, "r2", "bert")  # takes alias "b" in r2
        join(self.b, "r2", "bob")  # bob is "bo" there
        join(self.a, "r2", "alice")
        rooms.set_prefs(self.b, "r2", digest=True)
        post(self.a, "r2", "to b", kind="ans", re="b1")
        post(self.a, "r2", "to bo", kind="ans", re="bo1")
        lines = self.context(self.deliver(self.b)).split("\n")[1:]
        self.assertIn("a2 alice→all ans re=bo1: to bo", lines)
        self.assertIn("unthreaded: 1 new (a1), last alice: to b", lines)

    def test_a_reply_to_my_post_from_before_clear_stays_whole(self):
        post(self.b, "r", "my plan")  # b1, before the /clear
        new = new_sid()
        rooms.carry_over(self.b, new)
        rooms.set_prefs(new, "r", threads=[])  # a subscription that filters every thread
        post(self.a, "r", "ok", kind="ans", re="b1", thread="db")
        self.assertIn("a2 alice→all ans re=b1 #db: ok", self.context(self.deliver(new)))
```

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.test_render.BuildTest tests.test_hook_deliver -q`

Expected:
- the fallback test FAILS (context over 6,500 bytes, no `fallback`);
- `_fallback_line` errors (`AttributeError`);
- `test_a_fallback_line_is_not_delivery_evidence` FAILS (a list is returned, not a tuple);
- the idle-fire test FAILS (`save_emit` called);
- the future-evidence and future-receipt tests FAIL;
- the pinning tests PASS.

- [ ] **Step 3: Implement**

`plugin/lib/passnote/render.py`. Add after `_reserve_for_overflow`:

```python
def _fallback_line(it, multi) -> str:
    """build's first line when even MIN_CLIP doesn't fit (#31): a fixed shape whose only data is the id
    and the [room] prefix, each clipped to FIELD_CLIP before escaping, so it is at most ~2.4 KB as JSON
    and always fits the first line's cap. It still counts as emitted, so the cursor moves on and a
    forged line can't block a room; its text never reached the model, so it is never delivery evidence."""
    msg_id = escape_text(clip_field(it["msg"].get("id", "?")))
    line = f"{msg_id} (too large to show in this turn; passnote read --id {msg_id})"
    if multi:
        line = f"[{escape_text(clip_field(it.get('display') or it['room']))}] {line}"
    return line
```

In `build`, the first-line branch: set `extra = {}` just before the shrink `if`, and put this check after that `if` block ends (so it also covers a clip already at `MIN_CLIP`), replacing the current `lines.append` / `emitted.append` pair:

```python
            if size + _json_len("\n" + line) > first_cap:
                line, extra = _fallback_line(it, multi), {"fallback": True}
            lines.append(line)
            emitted.append(dict(it, line=line, **extra))
```

A line that fits is unchanged. Update `build`'s docstring: "the first line falls back to a fixed shape (marked `fallback`) when even MIN_CLIP doesn't fit".

`plugin/lib/passnote/hook.py`:

```python
def _valid_receipt(entry, rooms_joined, now) -> bool:
    """A pending receipt as track() records it; anything else in the emit state is dropped, and so is a
    ts in the future (#31): it would never age out."""
    ...
    return (... and _finite(entry.get("ts")) and entry["ts"] <= now and ...)
```

`_Fire.__init__`: `now = time.time()`, and the pending filter uses `_valid_receipt(entry, self.rooms, now)`.

```python
def _delivered(previous, emitted, now):
    """(evidence, added): this session's delivery evidence after a fire, and whether this fire added
    any. ... Only lines emitted whole count, never held, overflowing, digested or fallback ones (their
    text didn't reach the model) ... Bounded: the last MAX_DELIVERED, none older than RECEIPT_MAX_AGE
    or dated in the future; malformed entries are dropped."""
    out = [entry for entry in previous[-MAX_DELIVERED:]
           if _valid_evidence(entry) and 0 <= now - entry["ts"] <= RECEIPT_MAX_AGE]
    keys = {(entry["room"], entry["id"], entry["seq"]) for entry in out}
    added = False
    for it in emitted:
        msg = it["msg"]
        key = (it["room"], msg["id"], msg["seq"])
        if (msg.get("kind") in RECEIPT_KINDS and render.addressed_by_name(msg, it.get("me"))
                and not it.get("digest") and not it.get("fallback") and key not in keys):
            keys.add(key)
            out.append({"room": it["room"], "id": msg["id"], "seq": msg["seq"], "ts": now})
            added = True
    return out[-MAX_DELIVERED:], added
```

`_deliver_locked`, the emit block:

```python
    now = time.time()
    delivered, added = _delivered(state["delivered"], emitted, now)
    emit = {"emitted": [dict(it["ref"], line=it["line"]) for it in emitted],
            "overflow": [it["ref"] for it in overflow] + fire.unread,
            "ahead": [ref for room in fire.rooms for ref in fire.ahead.get(room, ())],
            "receipts": fire.pending,
            "delivered": state["delivered"]}
    if emit != state or added:
        # Evidence is pruned (aged out, future-dated, malformed) only in a save that happens anyway
        # (#31): pruning alone never writes on an otherwise idle fire.
        emit["delivered"] = delivered
        sessions.save_emit(sid, emit["emitted"], emit["overflow"], emit["ahead"], emit["receipts"],
                           emit["delivered"])
```

Keep the existing `pairs, fire.pending = _receipts(fire, time.time()) ...` line as is.

- [ ] **Step 4: Run the suite and lint**

Run: `python3 -m unittest discover -s tests -q && uvx ruff==0.16.10 check`
Expected: OK, 658 tests. If an existing evidence test expected pruning on an idle fire, update it to prune on a fire that saves anyway, and say so in the commit body.

- [ ] **Step 5: Docs and commit**

- Spec §7 step 6: after the first-line rule ("The first line of a fire… is always emitted"), add: "If even a 40-character clip doesn't fit, the first line is `<id> (too large to show in this turn; passnote read --id <id>)` (with the `[room]` prefix): emitted, so the cursor moves on, but never delivery evidence."
- Spec §7 seen receipts, delivery evidence bullet: "none older than 24 hours" becomes "none older than 24 hours or dated in the future; stale entries are pruned only in a save that happens anyway". In "Bounds": "24 hours (older entries, or entries dated in the future, are dropped unreported)".
- Spec v2.3 list: `- #31 (§7): a first line too large for any clip has a fixed shape; future-dated receipts and evidence are dropped; pruning never writes on an idle fire.`
- CHANGELOG `### Fixed`: `- The hook's output stays under its cap even when a forged message is too large to show at any clip; it is listed by id instead (#31).` and `- A turn with nothing new no longer rewrites the session's delivery state just to age out old entries (#31).`

```bash
git add -A plugin tests CHANGELOG.md docs/superpowers/specs/2026-09-26-passnote-design.md
git commit -m "Hook: fixed-shape fallback line, future ts dropped, no idle-fire writes (#31)

- render.build falls back to '<id> (too large to show in this turn;
  passnote read --id <id>)' when even MIN_CLIP doesn't fit: emitted
  (the cursor moves) but never delivery evidence.
- Pending receipts and delivery evidence dated in the future are
  dropped.
- Evidence is pruned only in a save that happens anyway.
- Pinning tests for the overflow line after a shrink, two addressed
  1,500-char posts, 64-character thread names under 8 KB, per-room
  alias wholeness and replies across /clear.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Post-side bounds (closes #31 R2, R3 and the write-order C2 test)

**Decisions 6 and 7 apply.**

**Files:**
- Modify: `plugin/lib/passnote/store.py`. Add `LOG_LINE_MAX`, `fit_text`, `record_overhead`; change the long-id error in `append_message`.
- Modify: `plugin/lib/passnote/cli.py` (`_post`).
- Test: `tests/test_store.py` (`FullTextTest`, `StoreTest`), `tests/test_cli.py` (`PostTest`).
- Docs: spec §6 `text`; CHANGELOG.

**Interfaces:**
- Produces:
  - `store.LOG_LINE_MAX = 64 * 1024`;
  - `store.fit_text(text: str, max_chars: int, budget: int) -> str`;
  - `store.record_overhead(rec: dict, alias: str) -> int`.

- [ ] **Step 1: Write the failing tests**

`tests/test_store.py`, `StoreTest`:

```python
    def test_fit_text_keeps_the_longest_prefix_under_both_limits(self):
        self.assertEqual(store.fit_text("abc", 2, 100), "ab")
        self.assertEqual(store.fit_text("漢" * 10, 100, 6 * 4), "漢" * 4)  # each is \uXXXX: 6 bytes
        self.assertEqual(store.fit_text("漢" * 10, 100, 5), "")
        self.assertEqual(store.fit_text("x" * 4000, 4000, store.LOG_LINE_MAX), "x" * 4000)
```

`FullTextTest` **(pinning, #31 C2 "full-text write order")**:

```python
    def test_the_full_text_file_exists_before_the_log_line_is_written(self):
        seen, real = [], store._write_record

        def check(fd, data):
            seen.append(os.path.exists(store.full_text_path("r", "a1")))
            return real(fd, data)

        with mock.patch.object(store, "_write_record", check):
            store.append_message("r", self.rec, "a", full_text="x" * 5000)
        self.assertEqual(seen, [True])
```

`tests/test_cli.py`, `PostTest`. Add `json` and `from passnote import hook` and `from support import hook_input` to the imports if missing:

```python
    def test_a_large_text_max_chars_still_writes_a_readable_log_line(self):
        with mock.patch.dict(os.environ, {"PASSNOTE_TEXT_MAX_CHARS": "100000"}):
            code, _, err = self.run_cli(self.a, "post", stdin="漢" * 50000)
        self.assertEqual(code, 0, err)
        with open(store.log_path("r"), "rb") as fh:
            raw = fh.read().splitlines()[-1]
        self.assertLessEqual(len(raw) + 1, store.LOG_LINE_MAX)
        logged = json.loads(raw)
        self.assertEqual(logged["full_chars"], 50000)
        with open(store.full_text_path("r", logged["id"]), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "漢" * 50000)
        out = hook.main("PostToolBatch", hook_input(self.b), self.env(self.b))
        self.assertIn("full text in", out)

    def test_the_default_config_logs_the_same_prefix_as_before(self):
        self.assertEqual(self.run_cli(self.a, "post", stdin="x" * 5000)[0], 0)
        self.assertEqual(store.iter_messages("r")[-1][1]["text"], "x" * 4000)

    def test_an_oversized_re_is_refused_and_writes_nothing(self):
        code, _, err = self.run_cli(self.a, "post", "--re", "a" * 70000, stdin="hi")
        self.assertEqual(code, 2)
        self.assertIn("too long for one log line", err)
        self.assertEqual(store.iter_messages("r"), [])

    def test_a_forged_huge_seq_gives_a_clear_error_for_a_long_post(self):
        forged = {"v": 1, "seq": 10 ** 18, "id": "z1", "from": "x", "sid": self.b, "to": "all", "kind": "say", "text": "x"}
        with open(store.log_path("r"), "ab") as fh:
            fh.write(json.dumps(forged).encode() + b"\n")
        code, _, err = self.run_cli(self.a, "post", stdin="y" * 5000)
        self.assertEqual(code, 2)
        self.assertIn("too long for a full-text file", err)
        self.assertNotIn("invalid message id", err)
        self.assertFalse(os.path.exists(store.full_text_dir("r")))
        self.assertEqual(self.run_cli(self.a, "post", stdin="short")[0], 0)
```

If `PostTest`'s joins leave earlier lines in the log, adjust `test_an_oversized_re...` to compare the line count before and after.

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.test_store tests.test_cli.PostTest -q`

Expected:
- `fit_text` errors (`AttributeError`);
- the large-max test FAILS (a ~300 KB line, no `full_chars`);
- the `--re` test FAILS (exit 0);
- the seq test FAILS (the message says "invalid message id");
- the default-prefix and write-order tests PASS (pinning).

- [ ] **Step 3: Implement**

`plugin/lib/passnote/store.py`:

```python
# The longest log line a post writes (#31), measured on the encoded record: far below MAX_READ, so no
# setting (text_max_chars) and no text (4 bytes of UTF-8 can cost 12 escaped) makes a line read_from
# skips. A default post stays well under it: 4,000 characters are at most ~48 KB escaped.
LOG_LINE_MAX = 64 * 1024


def _json_len(s) -> int:
    return len(json.dumps(s, ensure_ascii=True)) - 2


def fit_text(text, max_chars, budget) -> str:
    """The longest prefix of `text`, at most `max_chars` characters, whose JSON-escaped size is at most
    `budget` bytes (a binary search; escaping is additive per character)."""
    prefix = text[:max_chars]
    if _json_len(prefix) <= budget:
        return prefix
    lo, hi = 0, len(prefix)  # prefix[:lo] fits, prefix[:hi] doesn't
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _json_len(prefix[:mid]) <= budget:
            lo = mid
        else:
            hi = mid
    return prefix[:lo]


def record_overhead(rec, alias) -> int:
    """Bytes of rec's log line without its text, with the largest seq, id, ts and full_chars a post
    gets, plus the newline and a possible repair newline (append_message)."""
    probe = dict(rec, text="", v=1, seq=10 ** 18, id=f"{alias}{10 ** 18}", ts=time.time(), full_chars=10 ** 6)
    return len(json.dumps(probe, ensure_ascii=True, sort_keys=True)) + 2
```

In `append_message`, replace the `written is None` raise:

```python
            if written is None:
                raise paths.PassnoteError(
                    f"message ids in {room} have grown too long for a full-text file (the log's last seq has "
                    f"{len(str(rec['seq'] - 1))} digits; a forged line?): post at most {len(rec['text'])} "
                    "characters, or ask the human to check the room's log", 2)
```

`plugin/lib/passnote/cli.py`, `_post`:
- delete the `full_text = None` / `if len(text) > cfg["text_max_chars"]` block;
- build `rec` as today, with `"text": text`;
- just before `msg = store.append_message(...)`, add:

```python
    # The log keeps the longest prefix that fits text_max_chars and LOG_LINE_MAX bytes (#27, #31); the
    # whole text goes to the message's full-text file, so no setting can make the line one the hook skips.
    overhead = store.record_overhead(rec, me["alias"])
    if overhead >= store.LOG_LINE_MAX:
        raise paths.PassnoteError("the post's other fields (--re, --to) are too long for one log line", 2)
    rec["text"] = store.fit_text(text, cfg["text_max_chars"], store.LOG_LINE_MAX - overhead)
    full_text = text if rec["text"] != text else None
```

The empty-text check, the `full_text_max_chars` cap (exit 4) and the secret guard stay before this, unchanged. The `ok <id> (full text: …)` line keeps using `full_text is not None`.

- [ ] **Step 4: Run the suite and lint**

Run: `python3 -m unittest discover -s tests -q && uvx ruff==0.16.10 check`
Expected: OK, 664 tests.

- [ ] **Step 5: Docs and commit**

- Spec §6, the `text` field bullet, append: "A log line is at most 64 KB (`LOG_LINE_MAX`), measured encoded: a text whose escaped prefix wouldn't fit keeps a shorter one and gets a full-text file, whatever `text_max_chars` is; a post whose other fields alone exceed it is refused (exit 2). When ids have grown too long for a full-text file name (a forged huge `seq`), a long post is refused with a message saying so."
- Spec v2.3 list: `- #31 (§6): a log line is at most 64 KB encoded, whatever text_max_chars is.`
- CHANGELOG `### Fixed`: `- A large \`text_max_chars\` (\`PASSNOTE_TEXT_MAX_CHARS\`) with non-ASCII text no longer writes a log line too long for delivery: the log keeps a shorter prefix and the whole text goes to the full-text file (#31).` and `- A long post in a room whose log holds a forged huge \`seq\` is refused with a clear message (#31).`

```bash
git add -A plugin tests CHANGELOG.md docs/superpowers/specs/2026-09-26-passnote-design.md
git commit -m "post: log lines capped at 64 KB encoded; clear error on huge ids (#31)

The logged prefix is the longest that fits text_max_chars and
LOG_LINE_MAX bytes once escaped, so no setting makes a line the hook
skips; an oversized --re is refused. A forged huge seq now gets a
message that says what happened. Pinning test: the full-text file
exists before the log line is written.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Thread subscriptions under the lock, bounded lists (closes #31 R7, R8)

**Files:**
- Modify: `plugin/lib/passnote/rooms.py`. Add `change_threads`.
- Modify: `plugin/lib/passnote/cli.py`: `cmd_subscribe`, `cmd_unsubscribe`, `_my_threads` (reduce it to name validation), `_threads_line`, `cmd_who`, and a new `_thread_names` and `THREADS_SHOWN`.
- Test: `tests/test_cli.py` (`ThreadTest`).
- Docs: spec §10 `who` row; CHANGELOG.

**Interfaces:**
- Produces: `rooms.change_threads(sid, room, add=(), remove=(), every=False) -> list | None`. The new sorted thread list, or None for every thread. Exit 3 if not a member; exit 2 for a remove with no subscription.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py`, `ThreadTest` (alice is `self.a` and bob is `self.b`, both joined to `r`):

```python
    def _racing(self, threads):
        """A room_lock that first stores `threads` for alice: another subscribe that finished just before ours."""
        real = store.room_lock

        def racing(room, *args, **kwargs):
            members = store.load_members(room)
            members[self.a]["threads"] = threads
            store.save_members(room, members)
            return real(room, *args, **kwargs)

        return racing

    def test_subscribe_keeps_a_change_made_just_before_it_took_the_lock(self):
        with mock.patch.object(store, "room_lock", self._racing(["first"])):
            self.assertEqual(self.run_cli(self.a, "subscribe", "second")[0], 0)
        self.assertEqual(store.load_members("r")[self.a]["threads"], ["first", "second"])

    def test_unsubscribe_keeps_a_change_made_just_before_it_took_the_lock(self):
        self.run_cli(self.a, "subscribe", "x", "y")
        with mock.patch.object(store, "room_lock", self._racing(["x", "y", "z"])):
            self.assertEqual(self.run_cli(self.a, "unsubscribe", "y")[0], 0)
        self.assertEqual(store.load_members("r")[self.a]["threads"], ["x", "z"])

    def test_who_and_subscribe_list_at_most_8_threads(self):
        members = store.load_members("r")
        members[self.a]["threads"] = [f"t{i:02d}" for i in range(100)]
        store.save_members("r", members)
        _, out, _ = self.run_cli(self.a, "who")
        self.assertIn("· threads t00, t01, t02, t03, t04, t05, t06, t07, +92", out)
        _, out, _ = self.run_cli(self.a, "subscribe")
        self.assertEqual(out, "threads in r: t00, t01, t02, t03, t04, t05, t06, t07, +92\n")
```

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.test_cli.ThreadTest -q`
Expected: all three FAIL. The first two lose `"first"`/`"z"`; the third prints 100 names.

- [ ] **Step 3: Implement**

`plugin/lib/passnote/rooms.py`, after `set_prefs`:

```python
def change_threads(sid, room, add=(), remove=(), every=False):
    """Change this member's subscription (#25) with one read-modify-write under the room lock (#31), so
    two concurrent subscribe/unsubscribe commands never lose each other's change. `every` removes the
    filter; `remove` drops threads (exit 2 when there is no subscription); otherwise `add` adds them.
    Returns the new sorted thread list, or None for every thread. Exit 3 if not a member."""
    with store.room_lock(room):
        members = store.load_members(room)
        if sid not in members:
            raise paths.PassnoteError(f"not joined to {room}; run: passnote join", 3)
        entry = members[sid]
        current, _ = store.member_prefs(entry)
        if every:
            entry.pop("threads", None)
            threads = None
        elif remove:
            if current is None:
                raise paths.PassnoteError("you get every thread; subscribe to the ones you want instead", 2)
            threads = sorted(current - set(remove))
            entry["threads"] = threads
        else:
            threads = sorted((current or frozenset()) | set(add))
            entry["threads"] = threads
        store.save_members(room, members)
        return threads
```

`plugin/lib/passnote/cli.py`:

```python
# Thread names `who` and `subscribe` print per member: a forged members.json list can be any length.
THREADS_SHOWN = 8


def _thread_names(threads) -> str:
    names = sorted(threads)
    shown = ", ".join(names[:THREADS_SHOWN])
    return shown + (f", +{len(names) - THREADS_SHOWN}" if len(names) > THREADS_SHOWN else "")
```

- `_threads_line`: `shown = _thread_names(threads) or "none (you still get …)"`. Keep the existing "none" text.
- `cmd_who`: `line += " · threads " + (_thread_names(threads) or "none")`.
- `cmd_subscribe`:
  - validate names (`paths.check_name`), then `sid, meta = _session(env)`, `room = _room(args, meta)`;
  - `--all`: `threads = rooms.change_threads(sid, room, every=True)`;
  - names: `threads = rooms.change_threads(sid, room, add=args.threads)`;
  - no arguments: `threads, _ = store.member_prefs(_members_or_exit(sid, room)[sid])` (a read only).
- `cmd_unsubscribe`: validate names, then `threads = rooms.change_threads(sid, room, remove=args.threads)`.
- Remove `_my_threads` if nothing else uses it.
- Keep `rooms.set_prefs` (digest uses it).

- [ ] **Step 4: Run the suite and lint**

Run: `python3 -m unittest discover -s tests -q && uvx ruff==0.16.10 check`
Expected: OK, 667 tests. The existing `unsubscribe` with no subscription still exits 2 with the same message.

- [ ] **Step 5: Docs and commit**

- Spec §10 `who` row: "thread subscription" becomes "thread subscription (at most 8 names, then +N)".
- Spec v2.3 list: `- #31 (§10): subscribe/unsubscribe change the set under the room lock; who and subscribe show at most 8 thread names.`
- CHANGELOG `### Fixed`: `- Two \`subscribe\`/\`unsubscribe\` commands at once no longer lose one of the changes, and \`who\` lists at most 8 threads per member (#31).`

```bash
git add -A plugin tests CHANGELOG.md docs/superpowers/specs/2026-09-26-passnote-design.md
git commit -m "Threads: one read-modify-write under the room lock; bounded lists (#31)

subscribe and unsubscribe now read the current set inside the room lock
(rooms.change_threads), so concurrent changes are never lost. who and
subscribe print at most 8 thread names, then +N.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Room labels on seen receipts (closes #31 P1)

**Decision 8 applies.**

**Files:**
- Modify: `plugin/lib/passnote/render.py`: `receipt_pair_ok`, `_receipt_text`, `receipt_parts`, `receipt_line`.
- Modify: `plugin/lib/passnote/hook.py` (`_receipts`).
- Test: `tests/test_render.py` (`ReceiptLineTest`), `tests/test_hook_deliver.py` (`ReceiptTest`).
- Docs: README:77; spec §7 seen receipts "Output"; CHANGELOG.

**Interfaces:**
- Produces:
  - `render.receipt_parts(pairs) -> (line | None, shown)` and `render.receipt_line(pairs)`. Each pair is `(name, id)` or `(name, id, room_label)`; a label of None means no prefix;
  - `render.receipt_pair_ok(name, msg_id, room=None) -> bool`.
  - Two-tuples keep working, so existing tests and callers need no change.

- [ ] **Step 1: Write the failing tests**

`tests/test_render.py`, `ReceiptLineTest`:

```python
    def test_receipts_name_their_room_when_given(self):
        self.assertEqual(render.receipt_line([("bob", "a1", "r1"), ("bob", "a3", "r1"), ("dan", "a1", "r2")]),
                         "passnote: seen [r1] by bob: a1, a3; [r2] by dan: a1")
        self.assertIsNone(render.receipt_line([("bob", "a1", "bad room")]))
        self.assertEqual(render.receipt_line([("bob", "a1", None)]), "passnote: seen by bob: a1")
        self.assertLessEqual(len(render.receipt_line([("b" * 64, "a" * 72 + "9" * 18, "r" * 64)])),
                             render.RECEIPT_MAX_CHARS)  # a lone valid triple always fits
```

`tests/test_hook_deliver.py`, `ReceiptTest`:

```python
    def test_a_sender_in_two_rooms_gets_room_labelled_receipts(self):
        join(self.a, "r2", "alice")
        join(self.b, "r2", "bob")
        post(self.a, "r", "q1", kind="ask", to=["bob"])
        post(self.a, "r2", "q2", kind="ask", to=["bob"])
        self.deliver(self.a)  # alice's cursors pass both: pending
        self.deliver(self.b)  # bob's turn delivers both
        self.assertIn("passnote: seen [r] by bob: a1; [r2] by bob: a1", self.context(self.deliver(self.a)))
```

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m unittest tests.test_render.ReceiptLineTest tests.test_hook_deliver.ReceiptTest -q`
Expected: the render test FAILS (a 3-tuple unpack error or a missing prefix); the hook test FAILS (`passnote: seen by bob: a1, a1`).

- [ ] **Step 3: Implement**

`plugin/lib/passnote/render.py`:

```python
def receipt_pair_ok(name, msg_id, room=None) -> bool:
    """Whether a (name, id[, room label]) may appear in a receipt line: a valid member name, a message id
    of the full-text id shape, and a label that is None or a valid name. All ASCII, needing no escaping."""
    return (paths.valid_name(name) and isinstance(msg_id, str) and bool(store.FULL_TEXT_ID_RE.fullmatch(msg_id))
            and (room is None or paths.valid_name(room)))


def _receipt_text(by_room) -> str:
    """by_room: {room label or None: {name: [ids]}}, in first-seen order."""
    parts = []
    for room, by_name in by_room.items():
        names = "; ".join(f"by {name}: {', '.join(ids)}" for name, ids in by_name.items())
        parts.append(f"[{room}] {names}" if room else names)
    return "passnote: seen " + "; ".join(parts)


def receipt_parts(pairs):
    """(line, shown): the receipt line for (name, id) or (name, id, room label) pairs, grouped by label
    then name in first-seen order, and the pairs it shows (as given). Invalid pairs are dropped. Pairs
    are added one at a time until the next would take the line past RECEIPT_MAX_CHARS; a lone valid
    pair always fits. (None, []) when none is valid."""
    by_room, shown = {}, []
    for pair in pairs:
        name, msg_id, room = (tuple(pair) + (None,))[:3]
        if not receipt_pair_ok(name, msg_id, room):
            continue
        trial = {label: {key: list(ids) for key, ids in names.items()} for label, names in by_room.items()}
        trial.setdefault(room, {}).setdefault(name, []).append(msg_id)
        if len(_receipt_text(trial)) > RECEIPT_MAX_CHARS:
            break
        by_room = trial
        shown.append(pair)
    return (_receipt_text(by_room) if shown else None), shown
```

`receipt_line` is unchanged (`return receipt_parts(pairs)[0]`). Update its docstring example.

`plugin/lib/passnote/hook.py`, `_receipts`:
- once, before the loop: `multi = len(fire.rooms) > 1`;
- per entry: `members, display, inbound, _ = fire.room(room)` and `label = (display if paths.valid_name(display) else room) if multi else None`;
- `found.append((kept, name, msg_id, label))`;
- `render.receipt_parts([(name, msg_id, label) for _, name, msg_id, label in found])`;
- the final loop unpacks four values, and its validity check is `render.receipt_pair_ok(name, msg_id, label)`.

The shown-count bookkeeping is otherwise unchanged.

- [ ] **Step 4: Run the suite and lint**

Run: `python3 -m unittest discover -s tests -q && uvx ruff==0.16.10 check`
Expected: OK, 669 tests. Every existing single-room receipt test passes unchanged.

- [ ] **Step 5: Docs and commit**

- README:77: after "`passnote: seen by <name>: <id>`", add "(in more than one room, each room's ids follow `[<room>]`)". Check `site/index.html` and `site/llms.txt` for the same sentence (`grep -n "seen by" site/`) and mirror the change.
- Spec §7 seen receipts, "Output": add "A session in more than one room gets `passnote: seen [r1] by bob: a12; [r2] by carol: b3`: the label is the room's display when it is a valid name, else the room id, so the line stays ASCII."
- Spec v2.3 list: `- #31 (§7): receipt lines carry [room] labels for a session in more than one room.`
- CHANGELOG `### Changed`: `- Seen receipts name the room (\`passnote: seen [<room>] by <name>: <id>\`) when you are in more than one room, since ids repeat across rooms (#31).`

```bash
git add -A plugin tests README.md site CHANGELOG.md docs/superpowers/specs/2026-09-26-passnote-design.md
git commit -m "Seen receipts name the room for a session in several rooms (#31)

Ids are alias+seq per room, so 'seen by bob: a1' was ambiguous for a
sender in two rooms. The label is the room display when it is a valid
name, else the room id, so the line stays ASCII; single-room receipt
lines are unchanged.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Issue comments (for the maintainer to post after merge)

**#6:** Fixed in this batch. The hook and `read` decide "own" through `trust.own` (members.json `prev_sids`). A forged `prev_sids` entry can affect only a departed sender's lines, and gives no more than a forged raw log line already could.

**#7:** Items 1, 3, 5, 6, 7 and 8 are fixed.
- Item 2 (the heal landing in another session after `/resume`) is deferred. Spec §5 / spike A2 record that resume runs in a new process, with its own by-pid record. A `carry_to` gate written only by SessionStart(clear) would also end today's heal when SessionStart never ran. Reopen with a spike showing an in-process `/resume`.
- Item 4 (matching the old meta's pid/token) is wontfix. Same-user processes are inside the threat model, and the meta's pid mirror is legitimately skipped on LockBusy, so the check would fail real carries closed.

**#9:** Documented as a known limit in spec §7 step 4 and the README ("Data and uninstall"). A real fix needs a log-identity field in the room meta and in every cursor (a format change), minted at every place a log is created. passnote never deletes a log, so this needs a hand deletion. Close as documented, or keep open as `enhancement` if the format change is wanted later.

**#31**, per bullet:
- Fixed with a test:
  - build cap fallback;
  - log-line clamp;
  - clearer error for a forged huge seq (bounding `last_seq` would number new posts below the forged line, and readers skip out-of-order lines);
  - future ts;
  - idle-fire write;
  - subscribe/unsubscribe under the lock;
  - `who` thread list bounded;
  - receipt `[room]` labels;
  - private names and atomic write;
  - all seven missing tests;
  - cosmetic items.
- `files/` symlink or mode: wontfix. Same posture as `log.jsonl`; `PASSNOTE_HOME` is ownership- and mode-checked, and only a same-user process can plant either.
- Full-text sanitising: wontfix. Bidi, zero-width and tag characters are legitimate content (RTL text, ZWJ emoji, flag tags), so stripping them would make the "full text" lossy. The file reaches the model through Read, like any untrusted file.
- SKILL.md digest bullet: already says "props, replies to yours, --wake lines, asks/errs/naks/ans to you stay whole".

## Shared-file matrix (for the controller)

The tasks run in order on one branch. Two tasks touch the same function only where listed here:

| File | Tasks | Overlap to watch |
|---|---|---|
| `hook.py` | 1, 2, 4, 5, 8 | `_deliver_locked`: Task 4 adds the `reminder` tail, Task 5 rewrites the emit block. `_receipts`: Task 1 (`paths.valid_sid`), Task 8 (labels). `_Fire`: Task 2 (`verdict`), Task 5 (`__init__`) |
| `render.py` | 1, 5, 8 | Task 1 renames `clip_field`, which Task 5's `_fallback_line` uses |
| `rooms.py` | 1, 3, 7 | Task 3 changes `carry_over`'s signature, which Task 4 calls |
| `cli.py` | 2, 6, 7 | `_post` (6) and the thread commands (7) are separate functions |
| `store.py` | 1, 6 | `append_message`: Task 1 (write call), Task 6 (error text) |
| `tests/test_hook_deliver.py` | 2, 5, 8 | New classes / methods only |
| spec, CHANGELOG | all | Append-only lists; Task 1 creates the v2.3 block |

## Self-review notes

- **Coverage.** Every row of the Triage table marked include has a task and a named test.
  - #7 items 2 and 4, #31 R4, P2 and P3, and #9's real fix are closed with reasons, in **Issue comments**.
  - #31's seven missing tests are placed: doorbell id → Task 1; overflow after shrink, two-post overflow, 8 KB threads, per-room alias, replies across /clear → Task 5; write order → Task 6.
- **Names.** These are used consistently across tasks:
  - `paths.valid_sid`, `paths.atomic_write_text`, `render.clip_field`;
  - `trust.own`;
  - `rooms.CARRY_LOCK_TIMEOUT`, `rooms.carry_over(..., lock_timeout=)`;
  - `hook.CARRY_BUDGET`, `hook.CARRY_MIN_RETRY`, `hook._clock`, `hook.REMINDER_MAX_JSON`;
  - `hook._delivered -> (list, bool)`;
  - `store.LOG_LINE_MAX`, `store.fit_text`, `store.record_overhead`;
  - `rooms.change_threads`.
- **8 KB budget with all tails.** A fallback line is at most ~2.4 KB of JSON. The tail is at most the receipt line (300 ASCII) plus the reminder (1,000). The first line's cap is `6,500 − overflow reserve (< 100) − OVERFLOW_ID_SLACK (300) − tail`, which is at least 4.8 KB, so the fallback always fits.
- **Test counts** (622 at base) are estimates per step. The rule is "the count only goes up, except Task 4's in-place rewrite".
