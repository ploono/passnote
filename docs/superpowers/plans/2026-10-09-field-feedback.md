# passnote field-feedback batch Implementation Plan

> **Note:** This plan was executed with controller rulings recorded during review. Where it differs, the design spec v2.2 amendments and the code are authoritative.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close six issues raised in multi-session pilots:
- #19: a doorbell with no message text.
- #20: a larger clip for messages addressed to the reader.
- #27: long posts saved to a full-text file.
- #28: seen receipts for the sender.
- #25: threads with per-member subscriptions.
- #26: a digest mode for a hub member.

**Architecture:** Every change stays inside the existing design: a file log, one hook fire per turn and no daemon.
- Threads (#25) are a new field on the message.
- Full-text files (#27) live under the room's directory.
- Receipts (#28) are worked out by the sender's own hook from state that already exists: the addressee's cursor, emit state and hold events.
- Subscriptions and digest mode are stored on the member's entry in `members.json`. The hook already reads that file on every fire, so the settings cost no extra I/O.
- All new context lines are counted inside `render.build`'s size accounting, so the 8 KB hook-output cap still holds.

**Tech Stack:**
- Python 3.9+, stdlib only (`plugin/lib/passnote/`).
- `unittest` (`tests/`).
- POSIX sh guard (unchanged).

**Spec:** `docs/superpowers/specs/2026-09-26-passnote-design.md`, with the glossary in `CONTEXT.md`. Each task amends the spec sections it changes. Read the issues with `gh issue view N -R ploono/passnote --comments`. The maintainer's decisions on #19 and #25 are in the issue comments, and this plan follows them.

## Global Constraints

**Code**
- Python 3.9 compatible. Every plugin module starts with `from __future__ import annotations`. No `match` statements, and no runtime `X | Y` types.
- Standard library only.

**Hook behaviour**
- The hook must stay near-zero cost when nothing is new. A fire with no new lines and no pending receipts may not read any file it doesn't read today.
- Every new line type (receipt, digest, full-text note) is counted inside `render.build`'s `MAX_CONTEXT_JSON` accounting. The whole hook output stays under 8 KB, whatever the input, including forged log lines and forged `members.json` entries.
- One rendered message is still exactly one line (the escaping invariant, spike A4). The message id stays the first token of a delivered line (after an optional `[room]` prefix), because `transcript._ids_in` relies on it.

**Safety**
- Holds and the secret guard stay fail-closed:
  - a held message is never injected, summarized in a digest, or reported as "seen";
  - the secret guard scans the full text of every post.
- Forged fields (`thread`, `full_chars`, a member's `threads` or `digest`) must never crash the hook. A forged value fails open to delivering more, never less. A path is never taken from a log field.
- New state-changing verbs (`subscribe`, `unsubscribe`, `digest`) are denied to subagents by `hook._WRITE_CMD`.

**Docs**
- Wherever behaviour changes, update all of these in the same task:
  - `README.md`;
  - `CHANGELOG.md`, under `## [Unreleased]`;
  - the design spec (bump the status line to v2.2 in Task 1 and list each amendment as you go);
  - `CONTEXT.md` (the glossary) for new terms;
  - site copy. Check `site/index.html`, `site/llms.txt` and `tests/test_site.py`; README image alt texts must stay identical to the site's (`test_diagrams_reuse_the_readme_alt_texts`).
- Keep `plugin/skills/passnote/SKILL.md` edits minimal. Its body is loaded into every session that uses the skill.
  - It is 3,847 bytes at base. Each task states its byte allowance. Measure with `wc -c plugin/skills/passnote/SKILL.md` before and after, and put the delta in the commit message body.
  - The whole batch may add at most 750 bytes.
- Vocabulary follows `CONTEXT.md`. Say "thread" (never "topic" or "channel"), "message" and "post". The doorbell text `passnote note waiting` is the maintainer's exact wording (#19) and is kept as is.

**Process**
- Test-first. For every step that changes behaviour: write the failing test, run it and see it fail, implement, run it and see it pass.
- Before every commit, the full suite and lint must pass:
  - `python3 -m unittest discover -s tests -q` (490 tests at base; the count only goes up);
  - `pipx run ruff==0.16.10 check`.
- New env vars go into `tests/support.py` `CLEAR_ENV`, so a developer's environment can't leak into the suite.
- The repo is public. Keep local paths, usernames and session ids out of code, docs, test fixtures and commit messages. Tests build paths from `self.home` and `paths.home()`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Never push or merge. The controller does that.

## Review Focus

These are the inputs most likely to break for a real user while no task's main-path tests cover them. Each line names the task that owns its test.
1. **A doorbell-woken turn must still deliver the text.** After #19 the receiver reads the text only from the hook. If `UserPromptSubmit` didn't fire on a turn that SendMessage started, the receiver would get "passnote note waiting" and nothing else. The field report on #19 says the hook did deliver the full text on such a turn. The opt-in integration test `test_doorbell_wakes_an_idle_receiver_that_accepts_cross_session_messages` (it asserts `MARIGOLD` reaches the receiver) now proves this path, because the doorbell no longer carries the text. **Task 1** keeps that assertion. The implementer runs the test if they have auth, and otherwise says so in the report.
2. **A long post must never produce a log line the hook skips.** `store.read_from` silently skips lines over 256 KB, and a 100,000-character CJK post would be about 600 KB once escaped. **Task 3** keeps only a prefix in the log and pins it with `test_a_huge_cjk_post_keeps_a_readable_log_line`.
3. **Forged fields from another member.** These are a `thread` that isn't a valid name, `full_chars` on an id like `../../x`, and `threads: "auth"` (a string) or `digest: "yes"` in `members.json`. Each must not crash, must not point outside the room, and must deliver at least as much as without the field. Owned by **Task 3** (`test_full_text_path_rejects_forged_ids`), **Task 5** (`test_forged_thread_values_count_as_unthreaded`, `test_forged_subscription_values_mean_all_threads`) and **Task 6** (`test_forged_digest_value_means_off`).
4. **`/clear` must keep the member's settings.** Subscriptions, digest mode and pending receipts must survive the carry-over to the new session id. Owned by **Task 4** (`test_pending_receipts_survive_clear`), **Task 5** (`test_subscription_survives_clear_and_rejoin`) and **Task 6** (`test_digest_survives_clear`).
5. **Digest lines and consent.** A digested message's emitted ref must be confirmed by its digest line in the transcript, otherwise it would be redelivered whole. An addressed `prop` must never be digested, because silence counts as consent once the cursor passes it. Owned by **Task 6** (`test_digest_line_is_confirmed_from_the_transcript`, `test_addressed_prop_arrives_whole_in_digest_mode`).

## File map (after all tasks)

| File | Responsibility | Tasks |
|---|---|---|
| `plugin/lib/passnote/wake.py` | doorbell line | 1 |
| `plugin/lib/passnote/config.py` | `clip_addressed_chars`, `full_text_max_chars` | 2, 3 |
| `plugin/lib/passnote/render.py` | per-item clip, full-text note, `#thread`, receipt line, digest lines, `tail` | 2, 3, 4, 5, 6 |
| `plugin/lib/passnote/store.py` | `full_text_path`, full-text write in `append_message`, `valid_message` fields, `member_prefs` | 3, 5, 6 |
| `plugin/lib/passnote/hook.py` | per-item clip, receipts, thread filter, digest marking, `_WRITE_CMD` | 2, 3, 4, 5, 6 |
| `plugin/lib/passnote/sessions.py` | emit state gains `receipts` | 4 |
| `plugin/lib/passnote/fold.py` | `passed()` helper shared with receipts | 4 |
| `plugin/lib/passnote/rooms.py` | `_carry_emit` receipts, `set_prefs`, join keeps prefs | 4, 5, 6 |
| `plugin/lib/passnote/cli.py` | post full text, `--thread`, `read --thread`, `who`, `subscribe`, `unsubscribe`, `digest` | 3, 5, 6 |
| `plugin/skills/passnote/SKILL.md` | protocol text | 1, 3, 4, 5, 6 |
| `README.md`, `CHANGELOG.md`, spec, `CONTEXT.md` | docs | all |
| `tests/support.py` | `CLEAR_ENV` | 2, 3 |

The tasks run in order on one branch. Later tasks build on the interfaces of earlier ones, listed in each task's **Interfaces** block.

---

### Task 1: Doorbell without message text (closes #19)

**Decision (maintainer, #19):** the doorbell carries only the id and the sender: `<id> from <sender>: passnote note waiting`. The receiver reads the text once, from the hook. This gives up showing a gist in Claude Code's native approve/deny prompt (spec F1).

**Decision: `post` still prints `WAIT <name> held (…)` for a post that may be held.** The old reason ("a doorbell would carry part of the text to the model") no longer applies. The new reason: Claude Code holds a cross-class doorbell natively anyway, and every hold, refuse or expiry notice wakes the sender for a full turn (about 13.9k tokens, spike A7), while the receiver would never see the message. The rule stays fail-closed, and the code is unchanged.

**Out of scope, with the reason, stated in the commit message and the CHANGELOG:**
- "A wake costs the sender at most one tool call" is not met.
  - The sender still makes one SendMessage call, plus a ToolSearch load in sessions where SendMessage is deferred.
  - Ringing from `post` itself needs the Phase D direct-socket doorbell (spec §13, #14). That is blocked under the sandbox (A5) and uses an interface Claude Code doesn't document.
- Batching is not possible, because SendMessage takes one `to`.
- The other half of #19's "Done when" is met: the receiver reads the text once.

**Files:**
- Modify: `plugin/lib/passnote/wake.py`. Replace `DOORBELL_GIST_CHARS` with `DOORBELL_TEXT`, rewrite `doorbell_line`, and update the docstring of `held`.
- Modify: `tests/test_wake.py`.
  - Delete `test_doorbell_gist_is_clipped_before_escaping`, `test_doorbell_gist_is_at_most_80_rendered_characters` and `test_doorbell_gist_ending_in_a_backslash_cannot_escape_the_quote`.
  - Rewrite `test_doorbell_line`.
  - Keep `test_doorbell_quotes_a_forged_id_safely`.
- Modify: `tests/test_cli.py:173` (`test_ask_waits_when_cold_and_wakes_when_warm`).
- Modify: `tests/integration/test_headless.py`. Add a comment only, above the `MARIGOLD` assertion: it now proves hook delivery on a doorbell-woken turn.
- Modify: `plugin/skills/passnote/SKILL.md` ("Waking a member" section; allowance +100 bytes).
- Modify: `README.md` (line 72, the held-doorbell reason), the spec (§8 post output contract, the F1 paragraph, and the status line), `CONTEXT.md` (Doorbell) and `CHANGELOG.md`.
- Check only: `site/index.html` and `site/llms.txt` contain no doorbell text and no gist claim (`grep -n -i "gist\|SendMessage(" site/`). Expect only the wake.svg alt text, which stays true. No site change.

**Interfaces:**
- Produces: `wake.DOORBELL_TEXT = "passnote note waiting"` and `wake.doorbell_line(name, msg) -> str`, with output `WAKE <name>: SendMessage(to="<name>", message="<id> from <sender>: passnote note waiting")`.
- Shares files with: Task 3 (`tests/test_cli.py`), and every task (SKILL.md, README.md, spec, CHANGELOG.md, CONTEXT.md).

- [ ] **Step 1: Write the failing tests** in `tests/test_wake.py` (class `DecideTest`), replacing the three gist tests and `test_doorbell_line`:

```python
    def test_doorbell_line_carries_no_message_text(self):
        m = {"id": "a7", "from": "alice", "text": 'the codeword is MARIGOLD "now"\nthen more'}
        line = wake.doorbell_line("bob", m)
        self.assertEqual(line, 'WAKE bob: SendMessage(to="bob", message="a7 from alice: passnote note waiting")')
        self.assertNotIn("MARIGOLD", line)

    def test_doorbell_quotes_a_forged_sender_safely(self):
        for sender in ('al"ice', "alice\\", "al\nice", "x" * 500):
            with self.subTest(sender=sender[:8]):
                line = wake.doorbell_line("bob", {"id": "a7", "from": sender, "text": "hi"})
                self.assertEqual(line.count('"'), 4)  # to="bob" and message="..." only
                self.assertEqual(len(line.splitlines()), 1)
                self.assertTrue(line.endswith(': passnote note waiting")'), line)
                self.assertLessEqual(len(line), 200)  # the sender is clipped to render.FIELD_CLIP
```

In `tests/test_cli.py:173`, change the expected line to:

```python
        self.assertIn('WAKE bob: SendMessage(to="bob", message="a2 from alice: passnote note waiting")', out)
        self.assertNotIn("again?", out.split("\n", 1)[1])
```

- [ ] **Step 2: Run them and see them fail.**
  Run: `python3 -m unittest tests.test_wake tests.test_cli -q`
  Expected: FAIL. The line still carries `a7 alice: <gist>`.

- [ ] **Step 3: Implement** in `plugin/lib/passnote/wake.py`:

```python
DOORBELL_TEXT = "passnote note waiting"  # no message text (#19): the receiver reads it once, from the hook


def doorbell_line(name, msg) -> str:
    # escape_text doubles every backslash, so no field can end in a lone "\" before the closing
    # quote; replacing '"' keeps each field inside message="...". Head fields are clipped like
    # rendered lines (render.FIELD_CLIP), so a forged sender can't make the line unbounded.
    sender = render.escape_text(render._clip_field(msg.get("from", "?"))).replace('"', "'")
    msg_id = render.escape_text(render._clip_field(msg.get("id", "?"))).replace('"', "'")
    return f'WAKE {name}: SendMessage(to="{name}", message="{msg_id} from {sender}: {DOORBELL_TEXT}")'
```

Then rewrite the docstring of `held()`. Delete "A doorbell carries a gist of the text, and a receiver with Claude Code's crossSessionInbound: accept would show it to the model, so a post that may be held never rings one". Put this in its place: "A post that may be held never rings a doorbell: Claude Code would hold it natively, each hold notice costs the sender a full turn (A7), and the receiver would not see the message anyway (spec §9)."

- [ ] **Step 4: Run them and see them pass.**
  Run: `python3 -m unittest tests.test_wake tests.test_cli -q`
  Expected: PASS.

- [ ] **Step 5: Update the docs.**
  - SKILL.md, under "Waking a member": after the `WAKE` bullet, add this sentence (about 95 bytes): `A doorbell you receive (\`<id> from <name>: passnote note waiting\`) has no text: the message is in your passnote block.` Leave the bullet's `message="…"` as it is.
  - README.md line 72: replace "because a doorbell would carry part of the text to the model" with "because Claude Code would hold the doorbell too, and each hold notice costs the sender a turn". In the same bullet, add a sentence: "A doorbell carries only the message id and sender (`<id> from <sender>: passnote note waiting`); the receiver reads the text from the hook, once."
  - Spec, status line: add `- v2.2 (2026-10-09) amends §4, §6, §7, §8 and §10 from field feedback (#19, #20, #27, #28, #25, #26).`
  - Spec §8 post output contract: change the WAKE line to `WAKE <name>: SendMessage(to="<name>", message="<id> from <from>: passnote note waiting")`.
  - Spec §8 F1 paragraph: replace "The doorbell carries the gist, not a bare PING, so Claude Code's native inbound approve/deny sees real content (F1)" with: "The doorbell carries no message text, only the id and sender (#19, amending F1). Pilots showed a receiver woken by a gist read the message twice, once clipped, and could act on the clipped copy before the whole one arrived through the hook. The cost: Claude Code's native approve/deny shows the sender and id, not content. The one-step doorbell (no SendMessage relay) remains Phase D."
  - `CONTEXT.md`, Doorbell: append "It carries the message id and sender, never the text."
  - CHANGELOG `[Unreleased]`: add a `### Changed` heading with: "Doorbells carry only the message id and sender (`<id> from <sender>: passnote note waiting`); the receiver reads the text once, from the hook (#19). A doorbell still costs the sender one SendMessage call; ringing from `post` directly waits for Phase D."

- [ ] **Step 6: Run the full suite and lint, and measure the skill.**
  Run: `python3 -m unittest discover -s tests -q && pipx run ruff==0.16.10 check && wc -c plugin/skills/passnote/SKILL.md`
  Expected: all pass. SKILL.md is at most 3,947 bytes.

- [ ] **Step 7: Commit.**

```bash
git add plugin/lib/passnote/wake.py tests/test_wake.py tests/test_cli.py tests/integration/test_headless.py \
  plugin/skills/passnote/SKILL.md README.md CHANGELOG.md CONTEXT.md docs/superpowers/specs/2026-09-26-passnote-design.md
git commit -m "Doorbell carries only the message id and sender (#19)" -m "SKILL.md +<N> bytes. One-step ringing stays Phase D (#14)." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

**Acceptance:**
- `post` never prints message text in a WAKE line.
- The held WAIT behaviour is unchanged.
- The spec records the F1 change and its reason.
- The suite and ruff pass.

---

### Task 2: Larger clip for messages addressed to the reader (closes #20)

**Decisions:**
- New config key `clip_addressed_chars`: default **1500**, env `PASSNOTE_CLIP_ADDRESSED_CHARS`. It sits with `clip_chars` in `DEFAULTS` and `ENV_INTS`. Reason: #20 asks for a typical 1,500-character addressed report to arrive whole.
- "Addressed to the reader" means the reader's name is in a list-valued `to`. A broadcast (`to: "all"`) keeps `clip_chars` (600). This holds for every kind.
- The addressed clip is `max(clip_chars, clip_addressed_chars)`. Reason: a user who raised `clip_chars` above 1,500 must not see addressed messages clipped harder than broadcasts.
- `render.build` keeps its signature. The hook puts the clip on each item as `it["clip"]`, and `build` falls back to its `clip` argument when an item has none. Reason: Tasks 3, 4 and 6 add per-item behaviour too, and one stable signature avoids four rewrites.
- `render_budget_chars` (2000), `MAX_CONTEXT_JSON` (6500) and the first-line shrink are unchanged. A second long addressed message overflows to the next fire.

**Files:**
- Modify: `plugin/lib/passnote/config.py` (`DEFAULTS`, `ENV_INTS`).
- Modify: `plugin/lib/passnote/render.py` (`build`: use `it.get("clip", clip)` in `rendered()` and as the first-line `cur_clip` start; add `addressed_by_name`).
- Modify: `plugin/lib/passnote/hook.py` (`_deliver_locked`: set `it["clip"]` before `render.build`).
- Modify: `tests/support.py` (add `"PASSNOTE_CLIP_ADDRESSED_CHARS"` to `CLEAR_ENV`).
- Test: `tests/test_config.py`, `tests/test_render.py` (`BuildTest`), `tests/test_hook_deliver.py` (new class `AddressedClipTest(DeliverCase)`).
- Docs: README "How it works" bullet 2, spec §7 step 6 and the §10 config table, CHANGELOG. SKILL.md: no change (0 bytes). Site: `grep -n "600\|clip" site/index.html site/llms.txt` should find nothing to change.

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `config.load()["clip_addressed_chars"] -> int`;
  - `render.addressed_by_name(msg, me) -> bool` (True iff `msg["to"]` is a list containing `me`);
  - item key `"clip": int`, read by `render.build`.
- Shares files with: Tasks 3–6 (`render.py`, `hook.py`), Task 3 (`config.py`, `tests/support.py`, `tests/test_config.py`).

- [ ] **Step 1: Write the failing tests.**

`tests/test_config.py`, `ConfigTest`:

```python
    def test_addressed_clip_default_and_env(self):
        self.assertEqual(config.load()["clip_addressed_chars"], 1500)
        write(os.path.join(self.home, "config.json"), {"clip_addressed_chars": 900})
        self.assertEqual(config.load()["clip_addressed_chars"], 900)
        os.environ["PASSNOTE_CLIP_ADDRESSED_CHARS"] = "1200"
        self.assertEqual(config.load()["clip_addressed_chars"], 1200)
```

`tests/test_render.py`, `BuildTest`:

```python
    def test_an_item_clip_overrides_the_default_clip(self):
        it = dict(item(msg(to=["bob"], text="x" * 1400)), clip=1500)
        ctx, emitted, overflow = render.build([it], "bob", 2000, 600)
        self.assertIn("x" * 1400, ctx)
        self.assertNotIn("passnote read --id", ctx)

    def test_an_item_without_a_clip_uses_the_default(self):
        ctx, _, _ = render.build([item(msg(text="x" * 1400))], "bob", 2000, 600)
        self.assertIn("x" * 600 + "… (+800 chars: passnote read --id a1)", ctx)

    def test_two_long_addressed_messages_still_share_one_budget(self):
        items = [dict(item(msg(id=f"a{i}", seq=i, to=["bob"], text="x" * 1400)), clip=1500) for i in (1, 2)]
        ctx, emitted, overflow = render.build(items, "bob", 2000, 600)
        self.assertEqual([it["msg"]["id"] for it in emitted], ["a1"])
        self.assertEqual([it["msg"]["id"] for it in overflow], ["a2"])
        self.assertIn("1 not shown yet: a2", ctx)

    def test_long_cjk_addressed_messages_keep_the_overflow_line(self):
        items = [dict(item(msg(id=f"a{i}", seq=i, to=["bob"], kind="ask", text="漢" * 1500)), clip=1500) for i in (1, 2)]
        ctx, emitted, overflow = render.build(items, "bob", 2000, 600)
        self.assertLessEqual(len(json.dumps(ctx, ensure_ascii=True)) - 2, render.MAX_CONTEXT_JSON)
        self.assertEqual(len(emitted), 1)  # the first line shrank to fit
        self.assertTrue(ctx.split("\n")[-1].startswith("… 1 not shown yet"))

    def test_addressed_by_name(self):
        self.assertTrue(render.addressed_by_name(msg(to=["bob", "carol"]), "bob"))
        self.assertFalse(render.addressed_by_name(msg(to="all"), "bob"))
        self.assertFalse(render.addressed_by_name(msg(to=["carol"]), "bob"))
        self.assertFalse(render.addressed_by_name(msg(to=["bob"]), None))
```

`tests/test_hook_deliver.py`, new class:

```python
class AddressedClipTest(DeliverCase):
    def test_an_addressed_report_of_1500_chars_arrives_whole(self):
        post(self.a, "r", "r" * 1500, kind="done", to=["bob"])
        ctx = self.context(self.deliver(self.b))
        self.assertIn("r" * 1500, ctx)
        self.assertNotIn("passnote read --id", ctx)

    def test_a_broadcast_keeps_the_600_char_clip(self):
        post(self.a, "r", "b" * 1500)
        self.assertIn("b" * 600 + "… (+900 chars: passnote read --id a1)", self.context(self.deliver(self.b)))

    def test_the_addressed_clip_is_configurable(self):
        os.environ["PASSNOTE_CLIP_ADDRESSED_CHARS"] = "800"
        post(self.a, "r", "r" * 1500, to=["bob"])
        self.assertIn("(+700 chars: passnote read --id a1)", self.context(self.deliver(self.b)))

    def test_a_larger_clip_chars_wins_for_addressed_messages(self):
        os.environ["PASSNOTE_CLIP_CHARS"] = "1800"
        os.environ["PASSNOTE_CLIP_ADDRESSED_CHARS"] = "1500"
        post(self.a, "r", "r" * 1700, to=["bob"])
        self.assertIn("r" * 1700, self.context(self.deliver(self.b)))
```

- [ ] **Step 2: Run them and see them fail.**
  Run: `python3 -m unittest tests.test_config tests.test_render tests.test_hook_deliver -q`
  Expected: FAIL (`KeyError: 'clip_addressed_chars'`, and the addressed text is clipped at 600).

  **Existing tests may break in Step 4, and that is expected.** Addressed lines now clip at 1,500, so fewer fit in one fire. Budget, overflow and look-ahead tests in `tests/test_hook_deliver.py` and `tests/test_render.py` that use long addressed asks may change their counts. Fix each failure on its own merits:
  - when the test is about the 600 clip, set `PASSNOTE_CLIP_ADDRESSED_CHARS=600` in that test only (or give its items `clip=600`);
  - otherwise, update the expectation and say why in the commit body.

  Never set it in `DeliverCase.setUp`, because that would hide the feature.

- [ ] **Step 3: Implement.**
  - `config.py`: add `"clip_addressed_chars": 1500` to `DEFAULTS` and `"clip_addressed_chars": "PASSNOTE_CLIP_ADDRESSED_CHARS"` to `ENV_INTS`.
  - `tests/support.py`: add the env var to `CLEAR_ENV`.
  - `render.py`:

```python
def addressed_by_name(msg, me) -> bool:
    to = msg.get("to")
    return isinstance(to, list) and me is not None and me in to
```

  In `build`, inside `rendered(it, clip_val)` callers: the first-line branch starts at `cur_clip = it.get("clip", clip)`, and the general branch calls `rendered(it, it.get("clip", clip))`.

  - `hook.py`, `_deliver_locked`, before `render.build`:

```python
    addressed_clip = max(cfg["clip_chars"], cfg["clip_addressed_chars"])
    for it in fire.items:
        it["clip"] = addressed_clip if render.addressed_by_name(it["msg"], it["me"]) else cfg["clip_chars"]
```

- [ ] **Step 4: Run them and see them pass.** Use the same command as Step 2. Expected: PASS.

- [ ] **Step 5: Update the docs.**
  - README bullet 2 becomes: "…deliver new lines (at most 2,000 characters per turn, addressed asks first; a message is clipped at 1,500 characters when it is addressed to you by name, 600 otherwise)…".
  - Spec §7 step 6: "A single message longer than its clip is clipped: 1,500 characters (`clip_addressed_chars`) when addressed to the receiver by name, else 600 (`clip_chars`); the larger of the two applies to addressed messages."
  - Spec §10 table: add the row `clip_addressed_chars | 1500`.
  - CHANGELOG `### Changed`: "Messages addressed to you by name are clipped at 1,500 characters (`clip_addressed_chars`, `PASSNOTE_CLIP_ADDRESSED_CHARS`) instead of 600; broadcasts and the 2,000-character budget per turn are unchanged (#20)."

- [ ] **Step 6: Run the full suite and lint.** `python3 -m unittest discover -s tests -q && pipx run ruff==0.16.10 check`

- [ ] **Step 7: Commit.** Message: `Clip addressed messages at 1,500 characters (#20)`, plus the Co-Authored-By line.

**Acceptance:**
- An addressed message of up to 1,500 characters arrives whole.
- Broadcasts clip at 600.
- The per-fire budget and the 8 KB cap are unchanged, and tests cover both.

---

### Task 3: Long posts saved to a full-text file (closes #27)

**Decisions:**
- **"The cap" is `text_max_chars` (4000).** It is the only cap a post meets when it is written. Delivery clips depend on the reader (Task 2, Task 6). Spec §6 (F14) already says larger text goes in a file. Posts of 4,000 characters or fewer are unchanged, byte for byte.
- **A new upper bound, `full_text_max_chars`: 100,000** (env `PASSNOTE_FULL_TEXT_MAX_CHARS`). A longer post still exits 4. This check comes before the secret scan, so a huge paste is refused for its length without scanning it all. Reason: the room's disk use stays bounded.
- **The secret guard scans the full text** before anything is written.
- **The log line holds the first `text_max_chars` characters plus `"full_chars": <int>`. The full text lives only in the file.** Reason: `store.read_from` silently skips lines over 256 KB, and the full text with `ensure_ascii` could exceed that.
- **File naming:** `rooms/<room>/files/<id>.txt`, UTF-8, the full text exactly as posted (after the existing trailing-newline strip).
  - It is written atomically (temp file plus `os.replace`) **inside the room lock in `store.append_message`, before the log line**, and unlinked if the append fails. So a delivered path always exists.
  - Mode 0600 in a 0700 `files/` directory, from the umask and `os.open(..., 0o600)`. It inherits the room's protection.
- **The path is never stored in the log.** It is derived at render time from the room and a validated id (`^[a-z]{1,72}[0-9]{1,18}$`; `rooms._alias` makes aliases of up to 64 letters plus a short tail, so 72 covers every real alias). A forged id gets no path and falls back to the `passnote read --id` note. The hook never stats the file.
- **Cleanup:** the file goes with the room. passnote has no room deletion today; `uninstall --purge` removes `rooms/`, files included. There is no separate expiry, consistent with the log (log rotation is a spec non-goal). The README says so.
- **The delivered line** is the clipped text, then `… (+N chars: full text in <absolute path>)`, where N = `full_chars` minus the characters shown.
- **`read --id`** prints the stored 4,000 characters, then the same note. It never prints the file, because Bash output is truncated around 30k characters. The reader opens the file with the Read tool.
- **Permissions:** a Read outside the project may prompt in default mode. README's suggested permissions gain `Read(~/.local/state/passnote/rooms/**)`. Bash's sandbox doesn't apply to Read.
- **`post` output line 1** becomes `ok <id> (full text: <path>)` for a long post, and stays `ok <id>` otherwise. The WAKE and WAIT lines are unchanged.
- **Valid messages:** `full_chars` must be absent, None, or an int ≥ 0 that is not a bool. A message whose `full_chars` is no larger than its text length renders as an ordinary message.

**Files:**
- Modify: `plugin/lib/passnote/config.py` (`full_text_max_chars`).
- Modify: `plugin/lib/passnote/store.py` (`FULL_TEXT_ID_RE`, `full_text_dir`, `full_text_path`, `_write_full_text`, `append_message(..., full_text=None)`, `valid_message`).
- Modify: `plugin/lib/passnote/render.py` (`render_line(msg, me, members, clip, room=None)`; `build` passes `it["room"]`).
- Modify: `plugin/lib/passnote/cli.py` (`_post` length, secret and full-text flow; `cmd_read` passes `room=room`; `_watch_emit` passes `room`).
- Modify: `tests/support.py` (`CLEAR_ENV` += `"PASSNOTE_FULL_TEXT_MAX_CHARS"`).
- Test: `tests/test_store.py` (new class `FullTextTest(HomeCase)`, and `ValidMessageTest`), `tests/test_render.py` (`RenderLineTest`, `BuildTest`), `tests/test_cli.py` (`PostTest`, `ReadTest`), `tests/test_hook_deliver.py` (new class `FullTextDeliverTest(DeliverCase)`), `tests/test_config.py`.
  - Rewrite `PostTest.test_errors`: its first assertion becomes `stdin="x" * 100001` → 4.
  - Rewrite `PostTest.test_the_length_cap_comes_before_the_secret_scan` to use `"x" * 100000`.
- Docs: SKILL.md (Rules bullet; allowance +40 bytes net), README, spec §4 storage tree, §6, §7 and §10, `CONTEXT.md` (new term **Full-text file**), CHANGELOG. Site: the "Stays on your computer" copy stays true, so no change.

**Interfaces:**
- Consumes: the `it["clip"]` convention from Task 2.
- Produces:
  - `store.FULL_TEXT_ID_RE`;
  - `store.full_text_path(room, msg_id) -> str | None`;
  - `store.append_message(room, rec, alias, full_text=None) -> dict` (the record gains `full_chars` when `full_text` is given);
  - `render.render_line(msg, me, members, clip, room=None) -> str`;
  - `config.load()["full_text_max_chars"] -> int`.
- Shares files with: Task 2 (`config.py`, `render.py`, `tests/support.py`), Task 5 (`cli.py` `_post`, `store.valid_message`, `render_line`), Task 1 (`tests/test_cli.py`).

- [ ] **Step 1: Write the failing tests.**

`tests/test_store.py`:

```python
class FullTextTest(HomeCase):
    def setUp(self):
        super().setUp()
        paths.ensure_home()
        self.rec = {"from": "alice", "sid": new_sid(), "to": "all", "kind": "say", "text": "x" * 4000, "mode": "default"}

    def test_append_with_full_text_writes_the_file_first(self):
        msg = store.append_message("r", self.rec, "a", full_text="x" * 5000)
        path = store.full_text_path("r", msg["id"])
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "x" * 5000)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o777, 0o700)
        logged = store.iter_messages("r")[-1][1]
        self.assertEqual((len(logged["text"]), logged["full_chars"]), (4000, 5000))

    def test_full_text_file_is_removed_when_the_append_fails(self):
        with mock.patch.object(store, "_write_record", side_effect=OSError(28, "No space left")):
            with self.assertRaises(OSError):
                store.append_message("r", self.rec, "a", full_text="x" * 5000)
        self.assertEqual(os.listdir(store.full_text_dir("r")), [])

    def test_full_text_path_rejects_forged_ids(self):
        for forged in ("../x", "a1/../../etc", "A1", "", None, 7, "a" * 73 + "1", "a1.txt", "a"):
            with self.subTest(forged=forged):
                self.assertIsNone(store.full_text_path("r", forged))
        self.assertTrue(store.full_text_path("r", "b12").endswith(os.path.join("rooms", "r", "files", "b12.txt")))

    def test_a_huge_cjk_post_keeps_a_readable_log_line(self):
        msg = store.append_message("r", dict(self.rec, text="漢" * 4000), "a", full_text="漢" * 100000)
        with open(store.log_path("r"), "rb") as fh:
            raw = fh.read().split(b"\n")[0]
        self.assertLess(len(raw), store.MAX_READ)
        self.assertEqual(store.read_from(store.log_path("r"), 0)[0][0][1], raw)
        self.assertEqual(msg["full_chars"], 100000)
```

In `ValidMessageTest`, add: `full_chars` of `"5"`, `True`, `-1` and `1.5` makes a message invalid; `5000` and `None` keep it valid. Use the existing `with_field` helper.

`tests/test_render.py`, `RenderLineTest`. The test home comes from `support`, which sets `PASSNOTE_HOME`; this module imports `support` only for `sys.path`, so `paths.home()` reflects the environment at call time. Wrap the tests in `mock.patch.dict(os.environ, {"PASSNOTE_HOME": "/nonexistent-home"})`, which is never created because render never touches the disk:

```python
    def test_a_long_message_points_at_its_full_text_file(self):
        with mock.patch.dict(os.environ, {"PASSNOTE_HOME": "/nonexistent-home"}):
            m = msg(text="z" * 4000, full_chars=5000)
            line = render.render_line(m, "bob", MEMBERS, 600, room="r")
            path = store.full_text_path("r", "a1")
        self.assertTrue(line.endswith(f"z… (+4400 chars: full text in {path})"), line[-120:])

    def test_unbounded_render_still_points_at_the_file(self):
        with mock.patch.dict(os.environ, {"PASSNOTE_HOME": "/nonexistent-home"}):
            line = render.render_line(msg(text="z" * 4000, full_chars=5000), None, MEMBERS, render.UNBOUNDED, room="r")
        self.assertIn("z" * 4000 + "… (+1000 chars: full text in ", line)

    def test_forged_full_text_fields_fall_back_to_read_id(self):
        with mock.patch.dict(os.environ, {"PASSNOTE_HOME": "/nonexistent-home"}):
            forged_id = render.render_line(msg(id="../x", text="z" * 700, full_chars=10 ** 9), "bob", MEMBERS, 600, room="r")
            too_small = render.render_line(msg(text="z" * 700, full_chars=3), "bob", MEMBERS, 600, room="r")
            no_room = render.render_line(msg(text="z" * 700, full_chars=5000), "bob", MEMBERS, 600)
        self.assertIn("passnote read --id ../x)", forged_id)
        self.assertNotIn("full text in", forged_id)
        for line in (too_small, no_room):
            self.assertIn("(+100 chars: passnote read --id a1)", line)
```

(Add `import os` and `from passnote import store` to `test_render.py`.)

`tests/test_cli.py`, `PostTest`:

```python
    def test_a_long_post_is_saved_to_a_full_text_file(self):
        code, out, _ = self.run_cli(self.a, "post", stdin="y" * 5000)
        path = store.full_text_path("r", "a1")
        self.assertEqual((code, out), (0, f"ok a1 (full text: {path})\n"))
        logged = store.iter_messages("r")[-1][1]
        self.assertEqual((logged["text"], logged["full_chars"]), ("y" * 4000, 5000))
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "y" * 5000)

    def test_a_post_at_the_cap_is_unchanged(self):
        code, out, _ = self.run_cli(self.a, "post", stdin="y" * 4000)
        self.assertEqual((code, out), (0, "ok a1\n"))
        self.assertNotIn("full_chars", store.iter_messages("r")[-1][1])
        self.assertFalse(os.path.exists(store.full_text_dir("r")))

    def test_the_full_text_cap_refuses_and_writes_nothing(self):
        self.assertEqual(self.run_cli(self.a, "post", stdin="x" * 100001)[0], 4)
        self.assertEqual(store.iter_messages("r"), [])
        self.assertFalse(os.path.exists(store.full_text_dir("r")))

    def test_the_secret_guard_checks_the_full_text(self):
        code, _, err = self.run_cli(self.a, "post", stdin="x" * 4500 + " API_KEY=abcd1234abcd1234abcd")
        self.assertEqual(code, 2)
        self.assertIn("secret", err)
        self.assertEqual(store.iter_messages("r"), [])
        self.assertFalse(os.path.exists(store.full_text_dir("r")))
```

`tests/test_cli.py`, `ReadTest`:

```python
    def test_read_id_points_at_the_full_text(self):
        self.run_cli(self.a, "post", stdin="y" * 5000)
        out = self.run_cli(self.b, "read", "--id", "a1")[1]
        self.assertIn("y" * 4000 + f"… (+1000 chars: full text in {store.full_text_path('r', 'a1')})", out)
```

`tests/test_hook_deliver.py`:

```python
class FullTextDeliverTest(DeliverCase):
    def test_a_long_post_is_delivered_with_its_path(self):
        me = store.load_members("r")[self.a]
        store.append_message("r", {"from": "alice", "sid": self.a, "to": ["bob"], "kind": "done", "text": "q" * 4000,
                                   "mode": "default"}, me["alias"], full_text="q" * 6000)
        ctx = self.context(self.deliver(self.b))
        self.assertIn("q" * 1500 + f"… (+4500 chars: full text in {store.full_text_path('r', 'a1')})", ctx)

    def test_forged_full_chars_keep_the_output_under_8kb(self):
        for i in range(60):
            post(self.a, "r", "漢" * 4000, full_chars=10 ** 12, to=["bob"], kind="ask")
        out = hook.main("PostToolBatch", hook_input(self.b), self.env(self.b))
        self.assertLess(len(out.encode()), 8192)
```

`tests/test_config.py`: `config.load()["full_text_max_chars"] == 100000`, and env `PASSNOTE_FULL_TEXT_MAX_CHARS=5000` gives 5000.

- [ ] **Step 2: Run them and see them fail.**
  Run: `python3 -m unittest tests.test_store tests.test_render tests.test_cli tests.test_hook_deliver tests.test_config -q`
  Expected: FAIL (`AttributeError: full_text_path`, and exit 4 for a 5,000-character post).

- [ ] **Step 3: Implement.**

`store.py`:

```python
FULL_TEXT_ID_RE = re.compile(r"[a-z]{1,72}[0-9]{1,18}")  # alias + seq; anything else gets no path


def full_text_dir(room):
    return os.path.join(paths.room_dir(room), "files")


def full_text_path(room, msg_id):
    """Where a long message's whole text lives, derived from the room and a validated id, never read
    from the log: any member can forge a log line, so a path field could point anywhere."""
    if not isinstance(msg_id, str) or not FULL_TEXT_ID_RE.fullmatch(msg_id):
        return None
    return os.path.join(full_text_dir(room), f"{msg_id}.txt")


def _write_full_text(path, text) -> None:
    directory = paths.makedirs(os.path.dirname(path))
    tmp = os.path.join(directory, f".tmp-{os.getpid()}-{os.urandom(6).hex()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
```

`append_message(room, rec, alias, full_text=None)`. Under the lock, after computing `rec["id"]`:

```python
        written = None
        if full_text is not None:
            rec["full_chars"] = len(full_text)
            written = full_text_path(room, rec["id"])
            _write_full_text(written, full_text)
        data = ...  # as today
        try:
            fd = _open_append(path)
            try:
                _write_record(fd, data)
            finally:
                os.close(fd)
        except BaseException:
            if written:
                try:
                    os.unlink(written)
                except FileNotFoundError:
                    pass
            raise
```

Add `import re` to store.py. In `valid_message`, add:

```python
    full = msg.get("full_chars")
    if full is not None and (isinstance(full, bool) or not isinstance(full, int) or full < 0):
        return False
```

`render.py` `render_line(msg, me, members, clip, room=None)`, replacing the clip block:

```python
    raw_text = str(msg.get("text", ""))
    full = msg.get("full_chars")
    path = None
    if room and isinstance(full, int) and not isinstance(full, bool) and full > len(raw_text):
        try:
            path = store.full_text_path(room, msg.get("id"))
        except paths.PassnoteError:
            path = None
    shown = raw_text[:clip] if len(raw_text) > clip else raw_text
    if path:
        text = f"{escape_text(shown)}… (+{full - len(shown)} chars: full text in {escape_text(path)})"
    elif len(raw_text) > clip:
        text = f"{escape_text(shown)}… (+{len(raw_text) - clip} chars: passnote read --id {msg_id})"
    else:
        text = escape_text(raw_text)
```

(Add `paths` to render's imports.) In `build.rendered`, call `render_line(it["msg"], it.get("me", me), it["members"], clip_val, room=it["room"])`.

`config.py`: `"full_text_max_chars": 100000` in `DEFAULTS`, and `"PASSNOTE_FULL_TEXT_MAX_CHARS"` in `ENV_INTS`.

`cli._post`: replace the length block. Signature: `_post(sid, room, text, kind, to_arg, re_id, wake_flag, urgent, allow_secret, stdout)`; Task 5 adds `thread=None`.

```python
    if len(text) > cfg["full_text_max_chars"]:
        raise paths.PassnoteError(f"text is {len(text)} chars (max {cfg['full_text_max_chars']}); "
                                  "write it to a file and post the path", 4)
    if not allow_secret:
        hit = trust.looks_secret(text)  # the full text, before anything is written
        ...
    full_text = None
    if len(text) > cfg["text_max_chars"]:
        full_text, text = text, text[:cfg["text_max_chars"]]
    ...
    msg = store.append_message(room, rec, me["alias"], full_text=full_text)
    stdout.write(f"ok {msg['id']}" + (f" (full text: {store.full_text_path(room, msg['id'])})" if full_text else "") + "\n")
```

`cmd_read`: `render.render_line(msg, me, members, render.UNBOUNDED, room=room)`. `_watch_emit` needs the room id: add a `room` parameter and pass it from both call sites in `cmd_watch`.

- [ ] **Step 4: Run them and see them pass.** Use the same command as Step 2.

- [ ] **Step 5: Update the docs.**
  - SKILL.md Rules: replace "For anything over about 4,000 characters, write a file and post its path." with "Over 4,000 characters, the full text is saved to a file and the line carries its path; read it with Read." This is about +20 bytes. In "Notes for the user", append `Read(~/.local/state/passnote/rooms/**)` to the allowlist (+42 bytes).
  - README "How it works": add the bullet "A post over 4,000 characters (up to 100,000) keeps its first 4,000 in the log; the whole text goes to `rooms/<room>/files/<id>.txt` (mode 0600), and the delivered line ends with that path. The file is removed with the room's data (`passnote uninstall --purge`)." Add `Read(~/.local/state/passnote/rooms/**)` to "Suggested permissions" in Setup notes.
  - Spec:
    - §4 storage tree: `rooms/<room>/files/<id>.txt  full text of a post over text_max_chars`.
    - §6 `text`: "capped at 4,000 characters in the log; a longer post (up to `full_text_max_chars`, 100,000) stores its first 4,000 plus `full_chars`, and its whole text in `files/<id>.txt`, written under the room lock before the log line. The path is derived from room and id, never stored (#27)."
    - §7 step 6: the full-text note.
    - §8: exit 4 now means over `full_text_max_chars`.
    - §10 table: add `full_text_max_chars | 100000`.
  - `CONTEXT.md`, under Messages: add **Full-text file**: "The file holding the whole text of a message too long for the log. Delivery shows its path. _Avoid_: attachment, spill." Code names follow it: `full_text_path`, `full_text_max_chars`.
  - CHANGELOG `### Added`: "Posts over 4,000 characters (up to 100,000) are saved whole to a file in the room's directory, and receivers get its path with the clipped text (#27)."

- [ ] **Step 6: Run the full suite and lint, and measure the skill.** Run `wc -c plugin/skills/passnote/SKILL.md`. The total added since base must be at most 170 bytes.

- [ ] **Step 7: Commit.** Message: `Save posts over 4,000 characters to a full-text file and deliver its path (#27)`, plus the skill byte delta and the Co-Authored-By line.

**Acceptance:**
- A post over 4,000 characters is delivered as clipped text plus a readable path, with no action from the poster.
- Posts of 4,000 characters or fewer are unchanged.
- The secret guard sees the full text.
- No log line can exceed the 256 KB read bound.

---

### Task 4: Seen receipts for the sender (closes #28)

**Decisions:**
- **Which messages:** the sender's own posts of kind `ask` or `prop` whose `to` is a list of names. Reason: an ask's sender waits for a reply, and for a `prop`, "seen" is exactly when silence starts to count as consent. Other kinds and broadcasts get no receipts.
- **No new message kind, and no writes from `post`.** The sender's own delivery hook notices its own `ask` and `prop` lines when its cursor passes them in `_read_room` (it reads them today and skips them as its own). It records them as pending receipts in its emit state. Only the hook, under the session `.lock`, ever writes that state, so `post` and a concurrent fire can't race.
- **"Seen" for (message, addressee)** reuses `fold`'s prop rule through one helper, `fold.passed(seq, cur)`: the addressee's cursor `seq` is at or past the message's seq. On top of that rule, all of the following must hold:
  - the message is not in the addressee's emit `overflow` (the cursor passes overflowed lines before they are rendered). An entry in the addressee's emit `ahead` counts as seen, because it was taken ahead and delivered before the cursor reached it;
  - `trust.content_hold({"sid": me, "mode": entry["mode"]}, recorded_mode(addressee), room inbound, {})` is None (fail-closed: the receiver's `PASSNOTE_ALLOW_BYPASS` is invisible here, so such a receiver is never reported);
  - no `hold` event exists for (id, addressee sid) in the room's events tail. That tail is parsed only when a candidate has passed the other checks, so it is parsed once per message and addressee at most.

  A held or departed addressee is dropped from the entry for good, never reported.
- **Bounds** (constants in `hook.py`):
  - `RECEIPT_KINDS = ("ask", "prop")`;
  - `MAX_PENDING_RECEIPTS = 16` messages (the oldest are dropped);
  - `MAX_RECEIPT_ADDRESSEES = 4` per message (the first 4 names of `to`, excluding the sender);
  - `RECEIPT_MAX_AGE = 86400` seconds (older entries are dropped unreported);
  - `RECEIPTS_PER_FIRE = 6` (message, name) pairs shown per fire. The rest stay seen and are shown next fire.
- **Cost:** with no pending entries, a fire does no extra I/O. With pending entries, it does at most 16 × 4 cursor reads and one `emit.json` read per addressee.
- **Output:** one line, `passnote: seen by bob: a12, a14; by carol: a12`, grouped by name in first-seen order.
  - Only names that pass `paths.valid_name` and ids that match `store.FULL_TEXT_ID_RE` appear. Others are dropped. That keeps the line ASCII, so its JSON size equals its length.
  - The line is capped at `render.RECEIPT_MAX_CHARS = 300`.
  - It goes into `additionalContext` (the sender's model needs it) through a new `tail` argument of `render.build`. That argument is counted inside `MAX_CONTEXT_JSON` and the budget, reserved before packing like the overflow line.
  - With no messages, the context is the receipt line alone, with no header. The receipt is passnote's own text, not a peer message.
  - It is not added to `systemMessage`. Reason: that keeps the 8 KB arithmetic (6,500 + 1,000) unchanged. The human sees "seen" in `passnote who`.
  - Receipt lines are never recorded for transcript confirmation. A lost receipt is not redelivered, so each is shown at most once.
- **Known gap (documented, not fixed):** asks posted before a `/clear` carry the old session id, so the new session doesn't track them. The pending entries already recorded are carried over.

**Files:**
- Modify: `plugin/lib/passnote/fold.py` (`passed(seq, cur)`; `fold()` props use it).
- Modify: `plugin/lib/passnote/sessions.py` (`load_emit` keys += `"receipts"`; `save_emit(sid, emitted, overflow, ahead=(), receipts=())`).
- Modify: `plugin/lib/passnote/rooms.py` (`_carry_emit` merges `receipts`).
- Modify: `plugin/lib/passnote/render.py` (`RECEIPT_MAX_CHARS`, `receipt_line(pairs)`, `build(..., tail=None)`).
- Modify: `plugin/lib/passnote/hook.py` (`_Fire.track`, `_Fire.pending`, `_receipts(fire)`, `_deliver_locked` state and tail).
- Test: `tests/test_fold.py`, `tests/test_render.py` (new class `ReceiptLineTest`), `tests/test_hook_deliver.py` (new class `ReceiptTest(DeliverCase)`), `tests/test_hook_lifecycle.py` (carry-over).
- **Existing tests that may change:** once an addressee has taken a turn, the sender's next fire is no longer empty. At planning time, `grep -n "deliver(self.a" tests/` found no test that delivers to the sender of an ask or prop after the addressee's turn. Re-run `grep -rn "assertIsNone(self.deliver(\|assertIsNone(out)" tests/` before Step 4. For any hit where the sid posted an addressed ask or prop earlier in the test, update the expectation and name the test in the commit body, so the reviewer doesn't read it as a weakened test.
- Docs: SKILL.md Rules "No acks" bullet (allowance +70 bytes), README "How it works", spec §7 (new "Seen receipts" paragraph) and §6 (prop "seen" now also tells the sender), `CONTEXT.md` (new term **Seen receipt**), CHANGELOG. Site: no copy covers this, so no change. delivery.svg's "with nothing new it exits at zero tokens" is still true.

**Interfaces:**
- Consumes: `store.FULL_TEXT_ID_RE` (Task 3) and `render.build`'s item `clip` (Task 2).
- Produces:
  - `fold.passed(seq, cur) -> bool`;
  - `render.receipt_line(pairs: list[tuple[str, str]]) -> str | None` (pairs are (name, id));
  - `render.build(items, me, budget, clip, tail=None)`;
  - `sessions.load_emit(sid)["receipts"]` (a list of `{"room", "id", "seq", "ts", "mode", "to": [names]}`);
  - `sessions.save_emit(sid, emitted, overflow, ahead=(), receipts=())`.
- Shares files with: Tasks 2, 3, 5 and 6 (`render.py`, `hook.py`), Tasks 5 and 6 (`rooms.py`).

- [ ] **Step 1: Write the failing tests.**

`tests/test_fold.py`:

```python
    def test_passed(self):
        self.assertTrue(fold.passed(3, {"seq": 3}))
        self.assertFalse(fold.passed(4, {"seq": 3}))
        for cur in (None, {}, {"seq": "9"}, {"seq": True}):
            self.assertFalse(fold.passed(1, cur))
```

`tests/test_render.py`:

```python
class ReceiptLineTest(unittest.TestCase):
    def test_format_groups_by_name(self):
        self.assertEqual(render.receipt_line([("bob", "a1"), ("bob", "a3"), ("carol", "a1")]),
                         "passnote: seen by bob: a1, a3; by carol: a1")
        self.assertIsNone(render.receipt_line([]))

    def test_invalid_names_and_ids_are_dropped_and_the_line_is_bounded(self):
        pairs = [("böb", "a1"), ("bob", "../x"), ("bob", "a2")] + [("n" * 64, f"a{i}") for i in range(50)]
        line = render.receipt_line(pairs)
        self.assertNotIn("ö", line)
        self.assertNotIn("../x", line)
        self.assertTrue(line.isascii())
        self.assertLessEqual(len(line), render.RECEIPT_MAX_CHARS)

    def test_build_with_only_a_tail(self):
        self.assertEqual(render.build([], "bob", 2000, 600, tail="passnote: seen by bob: a1"),
                         ("passnote: seen by bob: a1", [], []))

    def test_tail_is_counted_inside_the_context_cap(self):
        items = [item(msg(id=f"a{i}", seq=i, to=["bob"], kind="ask", text="漢" * 600)) for i in range(1, 40)]
        tail = render.receipt_line([("n" * 64, f"a{i}") for i in range(1, 7)])
        ctx, emitted, overflow = render.build(items, "bob", 2000, 600, tail=tail)
        self.assertTrue(ctx.endswith("\n" + tail))
        self.assertLessEqual(len(json.dumps(ctx, ensure_ascii=True)) - 2, render.MAX_CONTEXT_JSON)
        self.assertTrue(overflow)
        self.assertIn("not shown yet", ctx.split("\n")[-2])
```

`tests/test_hook_deliver.py`:

```python
class ReceiptTest(DeliverCase):
    def seen_line(self, sid):
        lines = [line for line in self.context(self.deliver(sid)).split("\n") if line.startswith("passnote: seen by")]
        return lines[0] if lines else None

    def test_the_sender_sees_one_line_after_the_addressee_gets_the_ask(self):
        post(self.a, "r", "review?", kind="ask", to=["bob"])
        self.assertIsNone(self.seen_line(self.a))   # tracked; bob hasn't had a turn
        self.deliver(self.b)
        self.assertEqual(self.seen_line(self.a), "passnote: seen by bob: a1")
        self.assertIsNone(self.seen_line(self.a))   # at most once per message

    def test_props_get_receipts_and_says_and_broadcasts_do_not(self):
        post(self.a, "r", "ship at 3", kind="prop", to=["bob"])
        post(self.a, "r", "fyi", kind="say", to=["bob"])
        post(self.a, "r", "anyone?", kind="ask")
        self.deliver(self.a)
        self.deliver(self.b)
        self.assertEqual(self.seen_line(self.a), "passnote: seen by bob: a1")

    def test_a_held_ask_is_never_reported_seen(self):
        sessions.update_meta(self.b, lambda meta: meta.update(permission_mode="bypassPermissions"))
        post(self.a, "r", "secret?", kind="ask", to=["bob"])
        self.deliver(self.a)
        self.deliver(self.b, mode="bypassPermissions")
        self.assertIsNone(self.seen_line(self.a))
        self.assertEqual(sessions.load_emit(self.a)["receipts"], [])

    def test_an_overflowed_ask_is_not_seen_until_it_is_delivered(self):
        os.environ["PASSNOTE_RENDER_BUDGET_CHARS"] = "300"
        join(self.c, "r", "carol")
        for i in range(3):
            post(self.c, "r", "c" * 150, kind="ask", to=["bob"])
        post(self.a, "r", "mine?", kind="ask", to=["bob"])
        self.deliver(self.a)
        self.deliver(self.b)                       # the cursor passes a4; a4 overflows
        self.assertIsNone(self.seen_line(self.a))
        self.drain(self.b)
        self.assertEqual(self.seen_line(self.a), "passnote: seen by bob: a4")

    def test_receipts_are_bounded_per_fire(self):
        for i in range(10):
            post(self.a, "r", f"q{i}", kind="ask", to=["bob"])
        self.deliver(self.a)
        self.drain(self.b)
        first, second = self.seen_line(self.a), self.seen_line(self.a)
        self.assertEqual(first.count(", ") + 1, 6)
        self.assertEqual(second.count(", ") + 1, 4)
        self.assertIsNone(self.seen_line(self.a))

    def test_pending_receipts_are_capped_and_expire(self):
        for i in range(20):
            post(self.a, "r", f"q{i}", kind="ask", to=["bob"])
        self.deliver(self.a)
        pending = sessions.load_emit(self.a)["receipts"]
        self.assertEqual(len(pending), 16)
        self.assertEqual(pending[0]["id"], "a5")    # the oldest four were dropped
        state = sessions.load_emit(self.a)
        aged = [dict(entry, ts=entry["ts"] - 86401) for entry in state["receipts"]]
        sessions.save_emit(self.a, state["emitted"], state["overflow"], state["ahead"], aged)
        self.drain(self.b)
        self.assertIsNone(self.seen_line(self.a))

    def test_an_addressee_who_left_is_dropped(self):
        post(self.a, "r", "q", kind="ask", to=["bob"])
        self.deliver(self.a)
        rooms.leave(self.b, "r")
        self.assertIsNone(self.seen_line(self.a))
        self.assertEqual(sessions.load_emit(self.a)["receipts"], [])

    def test_a_forged_member_key_drops_the_receipt_and_delivery_continues(self):
        post(self.a, "r", "q", kind="ask", to=["bob"])
        self.deliver(self.a)
        join(self.c, "r", "carol")
        with store.room_lock("r"):
            members = store.load_members("r")
            members["not-a-sid"] = dict(members.pop(self.b))  # bob's name, under a forged key
            store.save_members("r", members)
        post(self.c, "r", "still here")
        ctx = self.context(self.deliver(self.a))
        self.assertIn("still here", ctx)
        self.assertNotIn("seen by", ctx)
        self.assertEqual(sessions.load_emit(self.a)["receipts"], [])

    def test_no_receipt_work_when_nothing_is_pending(self):
        post(self.b, "r", "hi")
        with mock.patch.object(cursor, "load", wraps=cursor.load) as spy:
            self.deliver(self.a)
        self.assertEqual({call.args[0] for call in spy.call_args_list}, {self.a})

    def test_receipt_and_heavy_traffic_stay_under_8kb(self):
        for i in range(6):
            post(self.a, "r", "q", kind="ask", to=["bob"])
        self.deliver(self.a)
        self.drain(self.b)
        for i in range(60):
            post(self.b, "r", "漢" * 1500, kind="ask", to=["alice"])
        out = hook.main("PostToolBatch", hook_input(self.a), self.env(self.a))
        self.assertLess(len(out.encode()), 8192)
        self.assertIn("passnote: seen by bob:", json.loads(out)["hookSpecificOutput"]["additionalContext"])
```

(Import `rooms`, `join` and `hook_input` in the test module where missing.)

`tests/test_hook_lifecycle.py`:

```python
    def test_pending_receipts_survive_clear(self):
        # alice (self.a) has a pending receipt; a /clear carries it to the new sid
        post(self.a, "r", "q", kind="ask", to=["bob"])
        hook.main("PostToolBatch", hook_input(self.a), self.env(self.a))
        new = new_sid()
        rooms.carry_over(self.a, new)
        self.assertEqual([entry["id"] for entry in sessions.load_emit(new)["receipts"]], ["a1"])
```

(Adapt it to that module's existing setUp. It needs alice and bob joined to `r` with recorded modes, as `support.join` does.)

- [ ] **Step 2: Run them and see them fail.**
  Run: `python3 -m unittest tests.test_fold tests.test_render tests.test_hook_deliver tests.test_hook_lifecycle -q`
  Expected: FAIL.

- [ ] **Step 3: Implement.**

`fold.py`:

```python
def passed(seq, cur) -> bool:
    """Whether a member's cursor `cur` has passed the message with `seq` (the "seen" rule)."""
    s = cur.get("seq") if isinstance(cur, dict) else None
    return isinstance(s, int) and not isinstance(s, bool) and isinstance(seq, int) and s >= seq
```

In `fold()` props, replace the inline comparison with `passed(msg.get("seq", 0), cursors.get(addressee))`.

`sessions.py`: `load_emit` returns the keys `("emitted", "overflow", "ahead", "receipts")`, and `save_emit(sid, emitted, overflow, ahead=(), receipts=())` writes all four.

`rooms._carry_emit`: pass `merged(new_emit["receipts"], old_emit["receipts"])` as `receipts`.

`render.py`:

```python
RECEIPT_MAX_CHARS = 300


def receipt_line(pairs):
    """'passnote: seen by bob: a1, a3; by carol: a1' for (name, id) pairs, ASCII only (invalid names
    or ids are dropped), at most RECEIPT_MAX_CHARS long; None when nothing is left."""
    by_name = {}
    for name, msg_id in pairs:
        if paths.valid_name(name) and isinstance(msg_id, str) and store.FULL_TEXT_ID_RE.fullmatch(msg_id):
            by_name.setdefault(name, []).append(msg_id)
    parts, line = [], None
    for name, ids in by_name.items():
        candidate = "passnote: seen " + "; ".join(parts + [f"by {name}: {', '.join(ids)}"])
        if len(candidate) > RECEIPT_MAX_CHARS:
            break
        parts.append(f"by {name}: {', '.join(ids)}")
        line = candidate
    return line
```

The hook trims `pairs` to `RECEIPTS_PER_FIRE` before calling it. A pair `receipt_line` dropped for length stays pending (the hook compares the ids that made it into the line).

`build(items, me, budget, clip, tail=None)`:
- `if not items: return (tail or None), [], []`.
- Otherwise, before packing: `if tail: reserve += _json_len("\n" + tail); budget -= len(tail) + 1`.
- After the overflow line: `if tail: lines.append(tail)`.
- The overflow-line growth loop must compare against `MAX_CONTEXT_JSON - (_json_len("\n" + tail) if tail else 0)`.

`hook.py`:

```python
RECEIPT_KINDS = ("ask", "prop")
MAX_PENDING_RECEIPTS = 16
MAX_RECEIPT_ADDRESSEES = 4
RECEIPT_MAX_AGE = 86400
RECEIPTS_PER_FIRE = 6
```

In `_Fire.__init__(self, sid, meta, env, ahead=(), receipts=())`, set `self.pending = [entry for entry in receipts if _valid_receipt(entry, self.rooms)]`. Add `_Fire.track(room, msg)`:

```python
    def track(self, room, msg):
        """The sender's own addressed ask or prop, passed by its own cursor: wait to report it seen."""
        to = msg.get("to")
        if msg.get("kind") not in RECEIPT_KINDS or not isinstance(to, list):
            return
        names = [name for name in dict.fromkeys(to) if name != self.me][:MAX_RECEIPT_ADDRESSEES]
        if names and not any(e["room"] == room and e["id"] == msg["id"] for e in self.pending):
            self.pending.append({"room": room, "id": msg["id"], "seq": msg["seq"], "ts": msg.get("ts") or time.time(),
                                 "mode": msg.get("mode"), "to": names})
            self.pending = self.pending[-MAX_PENDING_RECEIPTS:]
```

In `_read_room`, right before `action, reason = fire.verdict(room, msg)`: `if msg["sid"] == fire.sid: fire.track(room, msg)`.

`_valid_receipt(entry, rooms)` checks: a dict; `room` in rooms; `id` a str; `seq` an int ≥ 1 (not a bool); `ts` a number; `to` a list of str.

`_receipts(fire, now)` returns `(pairs, remaining_pending)`:
- For each entry, drop it if `now - ts > RECEIPT_MAX_AGE`.
- `members, _, inbound, _ = fire.room(room)`, and map name to sid from `members`.
- For each name, drop it if it has no sid, **or if the sid fails `rooms._valid_sid`**. `members.json` keys aren't validated on load, and `cursor.load`, `sessions.load_emit` and `sessions.recorded_mode` all call `paths.check_sid`. One forged key would otherwise raise on every fire and stop **all** delivery to the sender. Otherwise:
  - `cur = cursor.load(target, room)`;
  - `emit = sessions.load_emit(target)` (read once per target per fire, cached in a dict);
  - `in_ahead` / `in_overflow` = whether any ref with that room and id is in those lists;
  - seen = `in_ahead or (fold.passed(seq, cur) and not in_overflow)`.
- If seen, apply the hold checks:
  - `trust.content_hold({"sid": fire.sid, "mode": entry["mode"]}, sessions.recorded_mode(target), inbound, {})`;
  - any `hold` event with this id and `to_sid == target` in `store.read_events(room)`, read lazily once per room per fire.
- If held, drop the name. Else, if fewer than `RECEIPTS_PER_FIRE` pairs are collected, append `(name, id)` and drop the name; otherwise keep it for the next fire.
- An entry with no names left is removed.

`_deliver_locked`:
- Build `_Fire(sid, meta, env, state["ahead"], state["receipts"])`.
- After the room loop: `pairs, fire.pending = _receipts(fire, time.time()) if fire.pending else ([], [])`, then `tail = render.receipt_line(pairs)`.
- If `tail` is None while `pairs` is non-empty (every pair failed validation), the pairs are still dropped.
- Pass `tail=tail` to `render.build`.
- Add `"receipts": fire.pending` to `emit`, and pass it to `save_emit`.
- When `context` is a tail only, the hook still emits `hookSpecificOutput`, as it does for any non-empty context.

- [ ] **Step 4: Run them and see them pass.** Use the same command as Step 2.

- [ ] **Step 5: Update the docs.**
  - SKILL.md Rules, the "No acks" bullet: replace `` `who` shows "seen".`` with `` your next turn shows `seen by <name>` for your asks and props.`` (about +40 bytes).
  - README "How it works": add the bullet "After an addressee's turn delivers your `ask` or `prop`, your next turn shows one line, `passnote: seen by <name>: <id>` (at most once per message, at most 6 per turn). No message is sent for it: your hook reads the addressee's cursor. A held message is never reported seen."
  - Spec §7: add a "Seen receipts (#28)" paragraph with the rule, the bounds and the "not in systemMessage" reason above.
  - `CONTEXT.md`, under Delivery and trust: add **Seen receipt**: "A line on a sender's next turn saying an addressee's turn has delivered the sender's ask or proposal. Worked out from the addressee's cursor; the addressee sends nothing. _Avoid_: ack, read receipt."
  - CHANGELOG `### Added`: "Seen receipts: a sender's next turn shows `passnote: seen by <name>: <id>` once its addressed ask or prop has been delivered (#28)."

- [ ] **Step 6: Run the full suite and lint, and measure the skill.** The total added since base must be at most 230 bytes.

- [ ] **Step 7: Commit.** Message: `Seen receipts for the sender's addressed asks and props (#28)`, plus the byte delta and the Co-Authored-By line.

**Acceptance:**
- After an addressee's turn delivers the sender's ask, the sender's next turn shows one short "seen" line for it, once.
- There is no extra I/O when nothing is pending.
- Held, overflowed and left cases are covered.

---

### Task 5: Threads with per-member subscriptions (closes #25)

**Decisions:**
- **(Maintainer, #25)** A thread is a field on the message, set by `post --thread <name>`. Members see every thread unless they subscribe to some. Untagged lines and lines addressed to the member by name always reach it.
- **Validation:** `paths.check_name(name)` with `member=True`, so the name pattern and the reserved names apply exactly as for member names. `--thread all` is refused.
- **A reply inherits its thread:** `post --re <id>` without `--thread` takes the thread of message `<id>` when that message is in the log and has a valid thread. Reason: a reply then reaches the thread's subscribers, who saw the ask.
- **`claim` gets no `--thread`** in this batch (YAGNI).
- **Storage:** a `threads` list on the member's own entry in `rooms/<room>/members.json`. Absent means every thread; `[]` means only unthreaded and addressed lines. The reasons:
  - the hook already loads `members.json` per room on every fire, so there is no extra I/O;
  - `rooms.carry_over` moves the entry as a whole on `/clear`;
  - `who` already reads it.

  How the setting behaves:
  - It is written by `rooms.set_prefs` under the room lock.
  - `rooms.join` keeps `threads` (and Task 6's `digest`) when the same session re-joins.
  - A takeover starts fresh, because a new session takes the name, not the old session's preferences.
  - `leave` drops it together with the entry.
- **Forged or invalid values fail open:**
  - a member `threads` value that isn't a list of valid names means "every thread";
  - a message `thread` that is not a str makes the message invalid (`valid_message`);
  - a str that isn't a valid name counts as unthreaded (always delivered, rendered without `#`).
- **Commands:**
  - `passnote subscribe [THREAD ...] [--all] [--room R]`:
    - with no arguments, it prints the current setting;
    - with threads, it adds them;
    - with `--all`, it removes the filter;
    - `--all` together with threads is exit 2;
    - outside a room it exits 3.
  - `passnote unsubscribe THREAD ... [--room R]` removes threads from the list. With no filter in place it exits 2: "you get every thread; subscribe to the ones you want instead".
  - Output is one line:
    - `threads in <room>: all`;
    - `threads in <room>: auth, db`;
    - `threads in <room>: none (you still get unthreaded lines and lines addressed to you)`.
- **Filtering happens in the hook's fire path** (`_Fire.verdict`), not in `trust.visibility`. `read` stays a full pull with its own `--thread` filter. Filtered lines still move the cursor; they are done for this member, and `read --thread` shows them.
- **Rendering:** `<id> <from>→<aud> <kind>[ re=<id>][ #<thread>]: <text>`. The id stays the first token.
  - `read --thread NAME` is an extra filter, applied before `--id`, `--since` and `--last`.
  - `who` adds `· threads auth, db` or `· threads none` to a member's line when a filter is set.
  - `watch` shows `#thread` through `render_line`.
- **The subagent guard** `_WRITE_CMD` gains `subscribe|unsubscribe` (and `digest` in Task 6; add it now to avoid touching the regex twice).

**Files:**
- Modify: `plugin/lib/passnote/store.py` (`valid_message`: `thread` absent, None or a str; `member_prefs(info) -> (threads: frozenset | None, digest: bool)`).
- Modify: `plugin/lib/passnote/rooms.py` (`set_prefs(sid, room, threads=_KEEP, digest=_KEEP)`; `join` keeps `threads` and `digest` on a same-session re-join).
- Modify: `plugin/lib/passnote/render.py` (`render_line` renders the `#thread`; `thread_of(msg) -> str | None`).
- Modify: `plugin/lib/passnote/hook.py` (`_Fire.prefs(room)`, the filter in `_Fire.verdict`, `_WRITE_CMD`).
- Modify: `plugin/lib/passnote/cli.py` (`post --thread`, reply inheritance, `read --thread`, `who` prefs, `cmd_subscribe`, `cmd_unsubscribe`).
- Test: `tests/test_store.py` (`ValidMessageTest`), `tests/test_render.py` (`RenderLineTest`), `tests/test_cli.py` (new class `ThreadTest(CliCase)`), `tests/test_cli_view.py` (`WhoTest`, `WatchTest`), `tests/test_hook_deliver.py` (new class `ThreadDeliverTest(DeliverCase)`), `tests/test_hook_lifecycle.py` (`_WRITE_CMD` cases, carry-over), `tests/test_rooms.py` (join keeps prefs, takeover doesn't).
- Docs: SKILL.md (one bullet under Post and one under "Join and look around"; allowance +200 bytes), README (line format, commands, the subagent-guard verb list in Trust and safety), spec §6 (field), §7 step 5 (filter) and §10 (CLI table), `CONTEXT.md` (**Thread**, **Subscription**), CHANGELOG. Site: delivery.svg's alt is unchanged (subscriptions are opt-in, and the alt must stay identical to README's). No site change.

**Interfaces:**
- Consumes: `render_line(..., room=None)` (Task 3), the item `clip` (Task 2) and `render.build(..., tail=None)` (Task 4).
- Produces:
  - message field `"thread": str`;
  - `render.thread_of(msg) -> str | None` (a valid name or None);
  - `store.member_prefs(info) -> tuple` (`frozenset` or None, and a bool);
  - `rooms.set_prefs(sid, room, threads=..., digest=...) -> dict` (the updated entry);
  - `_Fire.prefs(room) -> (threads, digest)`;
  - `cli._post(..., thread=None)`.
- Shares files with: Task 6 (`store.member_prefs`, `rooms.set_prefs`, `_Fire.prefs`, `who`, `_WRITE_CMD`), Tasks 2–4 (`render.py`, `hook.py`), Task 3 (`cli._post`).

- [ ] **Step 1: Write the failing tests.**

`tests/test_render.py`, `RenderLineTest`:

```python
    def test_thread_follows_kind_and_re(self):
        self.assertEqual(render.render_line(msg(thread="auth"), "bob", MEMBERS, 600), "a1 alice→all say #auth: hi")
        self.assertEqual(render.render_line(msg(thread="auth", kind="ans", re="b2", to=["bob"]), "bob", MEMBERS, 600),
                         "a1 alice→you ans re=b2 #auth: hi")

    def test_forged_thread_values_count_as_unthreaded(self):
        for forged in ("a b", "<x>", "all", "x" * 65, "", ".."):
            with self.subTest(forged=forged):
                self.assertIsNone(render.thread_of(msg(thread=forged)))
                self.assertEqual(render.render_line(msg(thread=forged), "bob", MEMBERS, 600), "a1 alice→all say: hi")
```

`tests/test_store.py`, `ValidMessageTest`: `thread` of `5`, `["a"]` or `True` makes the message invalid; `"auth"`, `"a b"` (still a str) and `None` keep it valid. In `member_prefs`:

```python
    def test_member_prefs(self):
        self.assertEqual(store.member_prefs({"name": "b"}), (None, False))
        self.assertEqual(store.member_prefs({"threads": ["auth", "db"], "digest": True}), (frozenset({"auth", "db"}), True))
        self.assertEqual(store.member_prefs({"threads": []}), (frozenset(), False))
        for forged in ("auth", ["a b"], [1], {"auth": 1}, None):
            self.assertEqual(store.member_prefs({"threads": forged})[0], None)  # forged: every thread
```

`tests/test_cli.py`:

```python
class ThreadTest(CliCase):
    def setUp(self):
        super().setUp()
        for sid, name in ((self.a, "alice"), (self.b, "bob")):
            self.run_cli(sid, "join", "r", "--as", name)
            set_mode(sid, "default")

    def test_post_thread_is_stored_and_validated(self):
        self.assertEqual(self.run_cli(self.a, "post", "--thread", "auth", stdin="hi")[0], 0)
        self.assertEqual(store.iter_messages("r")[-1][1]["thread"], "auth")
        for bad in ("a b", "all", "x" * 65, ".."):
            self.assertEqual(self.run_cli(self.a, "post", "--thread", bad, stdin="hi")[0], 2)

    def test_a_reply_inherits_the_thread(self):
        self.run_cli(self.a, "post", "--thread", "auth", "--to", "bob", "--kind", "ask", stdin="q")
        self.run_cli(self.b, "post", "--re", "a1", "--kind", "ans", "--to", "alice", stdin="yes")
        self.assertEqual(store.iter_messages("r")[-1][1]["thread"], "auth")
        self.run_cli(self.b, "post", "--re", "a1", "--thread", "db", stdin="moved")
        self.assertEqual(store.iter_messages("r")[-1][1]["thread"], "db")

    def test_subscribe_and_unsubscribe(self):
        self.assertEqual(self.run_cli(self.b, "subscribe")[1], "threads in r: all\n")
        self.assertEqual(self.run_cli(self.b, "subscribe", "auth", "db")[1], "threads in r: auth, db\n")
        self.assertEqual(store.load_members("r")[self.b]["threads"], ["auth", "db"])
        self.assertEqual(self.run_cli(self.b, "unsubscribe", "db")[1], "threads in r: auth\n")
        self.assertEqual(self.run_cli(self.b, "unsubscribe", "auth")[1],
                         "threads in r: none (you still get unthreaded lines and lines addressed to you)\n")
        self.assertEqual(self.run_cli(self.b, "subscribe", "--all")[1], "threads in r: all\n")
        self.assertNotIn("threads", store.load_members("r")[self.b])
        self.assertEqual(self.run_cli(self.b, "unsubscribe", "x")[0], 2)
        self.assertEqual(self.run_cli(self.b, "subscribe", "--all", "auth")[0], 2)
        self.assertEqual(self.run_cli(self.b, "subscribe", "a b")[0], 2)
        self.assertEqual(self.run_cli(self.c, "subscribe", "auth", "--room", "r")[0], 3)

    def test_read_thread_filter(self):
        self.run_cli(self.a, "post", "--thread", "auth", stdin="one")
        self.run_cli(self.a, "post", "--thread", "db", stdin="two")
        self.run_cli(self.a, "post", stdin="three")
        out = self.run_cli(self.b, "read", "--thread", "auth")[1]
        self.assertEqual(out, "a1 alice→all say #auth: one\n")
        self.assertEqual(self.run_cli(self.b, "read", "--thread", "a b")[0], 2)
```

`tests/test_hook_deliver.py`:

```python
class ThreadDeliverTest(DeliverCase):
    def setUp(self):
        super().setUp()
        post(self.a, "r", "auth news", thread="auth")
        post(self.a, "r", "db news", thread="db")
        post(self.a, "r", "plain news")
        post(self.a, "r", "db question", thread="db", kind="ask", to=["bob"])

    def test_a_subscriber_gets_its_threads_untagged_and_addressed_lines(self):
        rooms.set_prefs(self.b, "r", threads=["auth"])
        seen, _ = self.drain(self.b)
        self.assertEqual(seen, {"a1", "a3", "a4"})
        self.assertIsNone(self.deliver(self.b))  # a2 is behind the cursor for good

    def test_no_subscription_gets_everything(self):
        self.assertEqual(self.drain(self.b)[0], {"a1", "a2", "a3", "a4"})

    def test_forged_subscription_values_mean_all_threads(self):
        with store.room_lock("r"):
            members = store.load_members("r")
            members[self.b]["threads"] = "auth"
            store.save_members("r", members)
        self.assertEqual(self.drain(self.b)[0], {"a1", "a2", "a3", "a4"})

    def test_forged_thread_on_a_line_is_delivered(self):
        rooms.set_prefs(self.b, "r", threads=["auth"])
        post(self.a, "r", "odd", thread="../x")
        self.assertIn("a5", self.drain(self.b)[0])
```

`tests/test_rooms.py`:
- `test_rejoin_keeps_prefs_and_takeover_does_not`: `set_prefs(b, threads=["auth"], digest=True)`; `rooms.join(b, "r", "bob", root)` again → the entry keeps both. Then mark b gone (follow the pattern in `test_name_clash_gone_dead_pid_is_takeover`, `tests/test_rooms.py:80`); a new sid joins as "bob" → the entry has neither key.
- `test_subscription_survives_clear_and_rejoin`: `set_prefs(b, threads=["auth"])`; `carry_over(b, new)` → `load_members("r")[new]["threads"] == ["auth"]`.

`tests/test_hook_lifecycle.py`, the `_WRITE_CMD` test: add `"passnote subscribe auth"`, `"passnote unsubscribe auth"` and `"passnote digest on"` to `denied`, and `"passnote subscribers"`, `"passnote digest-x"` and `"passnote subscribe-x"` to `allowed`.

`tests/test_cli_view.py`:
- `WhoTest.test_who_shows_threads`: after `subscribe auth` by bob, `who` has `bob (b) · … · threads auth`.
- `WatchTest`: a threaded post renders with `#auth` in `watch --once` output.

- [ ] **Step 2: Run them and see them fail.**
  Run: `python3 -m unittest tests.test_render tests.test_store tests.test_cli tests.test_cli_view tests.test_hook_deliver tests.test_hook_lifecycle tests.test_rooms -q`

- [ ] **Step 3: Implement.**
  - `store.valid_message`: `if "thread" in msg and msg.get("thread") is not None and not isinstance(msg.get("thread"), str): return False`.
  - `store.member_prefs(info)`:

```python
def member_prefs(info):
    """(threads, digest) from a member entry: threads is a frozenset of valid names, or None for
    every thread (absent, or not a list of valid names: a forged value fails open); digest is True
    only for the JSON value true."""
    threads = info.get("threads") if isinstance(info, dict) else None
    if not (isinstance(threads, list) and all(paths.valid_name(t) for t in threads)):
        threads = None
    digest = isinstance(info, dict) and info.get("digest") is True
    return (frozenset(threads) if threads is not None else None), digest
```

  - `rooms.set_prefs`:

```python
_KEEP = object()


def set_prefs(sid, room, threads=_KEEP, digest=_KEEP) -> dict:
    """Set this member's delivery preferences in members.json (None removes the key). Exit 3 if not a member."""
    with store.room_lock(room):
        members = store.load_members(room)
        if sid not in members:
            raise paths.PassnoteError(f"not joined to {room}; run: passnote join", 3)
        entry = members[sid]
        for key, value in (("threads", threads), ("digest", digest)):
            if value is _KEEP:
                continue
            if value is None or value is False:
                entry.pop(key, None)
            else:
                entry[key] = value
        store.save_members(room, members)
        return dict(entry)
```

  - `rooms.join`: when building `members[sid]`, copy `threads` and `digest` from `members.get(sid, {})` if present. Not on takeover: that entry is under the other sid, which is deleted.
  - `render.thread_of(msg)`: return `msg.get("thread")` if `paths.valid_name(...)` and it is not reserved (`.lower() not in paths.RESERVED_NAMES`), else None. In `render_line`, after the `re=` part: `t = thread_of(msg)`, and `if t: head += f" #{t}"`. A valid name needs no escaping.
  - `hook._Fire.prefs(room)`: `store.member_prefs(self.room(room)[0].get(self.sid))`, cached per fire.
  - `_Fire.verdict`:

```python
    def verdict(self, room, msg):
        _, _, inbound, me = self.room(room)
        action, reason = trust.visibility(msg, self.sid, me, self.receiver_mode, inbound, self.env)
        if action == "deliver":
            threads, _ = self.prefs(room)
            thread = render.thread_of(msg)
            if threads is not None and thread is not None and thread not in threads \
                    and not render.addressed_by_name(msg, me):
                return "skip", None  # an unsubscribed thread: done for this member (#25)
        return action, reason
```

  - `_WRITE_CMD`: change `(?:post|claim|join|leave)` to `(?:post|claim|join|leave|subscribe|unsubscribe|digest)`.
  - `cli`:
    - The `post` parser gains `--thread`.
    - `cmd_post` validates it with `paths.check_name(args.thread)` when it is given.
    - In `_post(..., thread=None)`, build `by_id` before the append whenever `re_id` is set. If `thread` is None and `re_id` is in `by_id`, set `thread = render.thread_of(by_id[re_id])`. If `thread` is set, `rec["thread"] = thread`. (The existing `by_id` after the append is the same dict, so reuse it.)
    - The `read` parser gains `--thread`, outside the mutually exclusive group. After the visibility filter: `if args.thread: paths.check_name(args.thread); msgs = [m for m in msgs if m.get("thread") == args.thread]`.
    - `cmd_who`: after the mode, add `threads, digest = store.member_prefs(info)`; `if threads is not None: line += " · threads " + (", ".join(sorted(threads)) or "none")`. Task 6 adds digest here.
    - New parsers `subscribe` (`threads` nargs="*", `--all`, `--room`) and `unsubscribe` (`threads` nargs="+", `--room`), both `needs_home=True`.
    - `cmd_subscribe` and `cmd_unsubscribe` validate each name with `paths.check_name`, read the current set through `store.member_prefs(_members_or_exit(sid, room)[sid])`, compute the new sorted list, call `rooms.set_prefs`, and print the line formats above.

- [ ] **Step 4: Run them and see them pass.** Use the same command as Step 2.

- [ ] **Step 5: Update the docs.**
  - SKILL.md, under Post (about 120 bytes): `` - `--thread <name>` tags a thread; a reply keeps its ask's thread. `` Under "Join and look around" (about 80 bytes): `` - `passnote subscribe <thread>...` limits delivery to those threads (plus lines to you); `--all` undoes it. ``
  - README:
    - In "How it works", change the line format to `<id> <sender>→<you|all|names> <kind>[ re=<id>][ #<thread>]: <text>`, and add a paragraph on threads and subscribe/unsubscribe.
    - In Trust and safety, the subagent bullet lists `post`, `claim`, `join`, `leave`, `subscribe`, `unsubscribe` and `digest`.
  - Spec:
    - §6: the `thread` field and its validation.
    - §7 step 5: the subscription filter.
    - §10 CLI table: `post --thread`, `read --thread`, `subscribe`, `unsubscribe`.
    - §4 `members.json` line: `sid → {name, alias, joined_at, root, [threads], [digest]}`.
  - `CONTEXT.md`, under Messages:
    - **Thread**: "A name a message can carry, so members can follow some lines of a room and not others. _Avoid_: topic, channel, sub-room."
    - **Subscription**: "The threads a member chose to receive. Without one, a member gets every thread; with one, it still gets unthreaded lines and lines addressed to it."
  - CHANGELOG `### Added`: "Threads: `post --thread <name>`, `read --thread`, and `passnote subscribe` / `unsubscribe` to receive only some threads (#25)."

- [ ] **Step 6: Run the full suite and lint, and measure the skill.** The total added since base must be at most 430 bytes.

- [ ] **Step 7: Commit.** Message: `Threads and per-member subscriptions (#25)`, plus the byte delta and the Co-Authored-By line.

**Acceptance:**
- A member subscribed to one thread receives only that thread's lines plus untagged lines and anything addressed to it.
- A member with no filter sees everything.
- The thread is shown in delivery, `read`, `who` and `watch`.

---

### Task 6: Digest mode for a hub member (closes #26)

**Decisions:**
- **Turning it on:** the command `passnote digest on|off [--room R]` sets `digest: true` on the member's entry in `members.json`, through `rooms.set_prefs` (Task 5). Reasons:
  - a hub may want to switch it as its load changes mid-session;
  - it lives where subscriptions live, so it survives `/clear` the same way;
  - the hook needs no extra I/O to read it.

  No join flag and no config key.
- **What still arrives whole in digest mode:** messages addressed to the hub by name with kind `ask`, `err`, `prop` or `nak`, or posted with `--wake`. That is `render.whole(msg, me)`. Reasons:
  - asks and errs need action;
  - a `prop` must be read, because silence counts as consent once the cursor passes it (and Task 4 would report it seen);
  - a `nak` objects to the hub's own prop.
- **Everything else is digested:** broadcasts of any kind, and addressed `say`, `ans`, `done` and `claim`. Reason: in hub-and-spoke most traffic is reports addressed to the hub, which is exactly the context cost #26 targets.
- **Digest line format:** one line per (room, thread) with new activity, `#<thread>: <n> new (<first id>..<last id>), last <sender>: <gist>`.
  - For a single message: `#auth-refactor: 1 new (b19), last bob: plan ready for gate`.
  - Untagged lines group as `unthreaded: 3 new (a3..a7), last alice: …`.
  - The gist is `render.gist(text, DIGEST_GIST_CHARS)` with `DIGEST_GIST_CHARS = 60`.
  - The `[room]` prefix applies as for other lines when several rooms are delivered.
- **Bounds:** at most `DIGEST_MAX_LINES = 8` digest lines per fire. The items of further groups overflow to the next fire (named on the overflow line). Digest lines are counted inside the budget and `MAX_CONTEXT_JSON` like any line.
- **Order:** redelivered items first, then whole items by priority, then digest lines in order of their earliest seq.
- **Transcript confirmation:** every emitted digested item's ref records the exact digest line as its `line`. `transcript.unconfirmed` then confirms the whole group from that one line, and an unconfirmed group comes back once (marked `redelivered`) as a digest line again, not as whole messages.
- **Thread filter first, then digest:** an unsubscribed thread is skipped (Task 5) before digest marking.
- **Detail on demand:** `passnote read --thread <name> --last <n>` (Task 5), or `read --id`. The SKILL line says so.

**Files:**
- Modify: `plugin/lib/passnote/render.py` (`DIGEST_GIST_CHARS`, `DIGEST_MAX_LINES`, `whole(msg, me)`, `digest_line(group, multi)`; `build` groups items with `it.get("digest")`).
- Modify: `plugin/lib/passnote/hook.py` (`_Fire.deliver` sets `item["digest"]`).
- Modify: `plugin/lib/passnote/cli.py` (`cmd_digest`; `who` shows `· digest`).
- Test: `tests/test_render.py` (new class `DigestTest`), `tests/test_hook_deliver.py` (new class `DigestDeliverTest(DeliverCase)`), `tests/test_cli.py` (`DigestCliTest(CliCase)`), `tests/test_cli_view.py` (`WhoTest`), `tests/test_rooms.py` (`test_digest_survives_clear`).
- Docs: SKILL.md (one bullet under "Join and look around"; allowance +150 bytes), README, spec §7 (digest delivery) and §10 (CLI), `CONTEXT.md` (**Digest**), CHANGELOG. Site: no change (opt-in).

**Interfaces:**
- Consumes: `store.member_prefs`, `rooms.set_prefs` and `_Fire.prefs` (Task 5); `render.thread_of` (Task 5); `render.addressed_by_name` (Task 2); `build(..., tail=None)` (Task 4).
- Produces:
  - `render.whole(msg, me) -> bool`;
  - `render.digest_line(group: list[item], multi: bool) -> str`;
  - item key `"digest": bool`.
- Shares files with: Tasks 2–5 (`render.py`, `hook.py`), Task 5 (`cli.py` `who`, `rooms.py`).

- [ ] **Step 1: Write the failing tests.**

`tests/test_render.py`:

```python
class DigestTest(unittest.TestCase):
    def test_whole(self):
        for kind in ("ask", "err", "prop", "nak"):
            self.assertTrue(render.whole(msg(kind=kind, to=["bob"]), "bob"), kind)
        self.assertTrue(render.whole(msg(kind="say", to=["bob"], wake=True), "bob"))
        for m in (msg(kind="done", to=["bob"]), msg(kind="ans", to=["bob"]), msg(kind="ask"), msg(kind="prop", to=["carol"])):
            self.assertFalse(render.whole(m, "bob"))

    def test_digest_line_format(self):
        group = [item(msg(id="a3", seq=3, thread="auth", text="first")),
                 item(msg(id="b5", seq=5, sid="S-B", **{"from": "bob"}, thread="auth", text="plan ready for gate"))]
        self.assertEqual(render.digest_line(group, False), "#auth: 2 new (a3..b5), last bob: plan ready for gate")
        self.assertEqual(render.digest_line(group[:1], False), "#auth: 1 new (a3), last alice: first")
        self.assertTrue(render.digest_line([item(msg(text="x" * 200))], False).startswith("unthreaded: 1 new (a1), last alice: "))
        self.assertLessEqual(len(render.digest_line([item(msg(text="x" * 200))], False).split(": ", 2)[2]), 60)

    def test_build_digests_and_keeps_whole_items_first(self):
        items = [dict(item(msg(id="a1", seq=1, thread="auth", text="s1")), digest=True),
                 dict(item(msg(id="a2", seq=2, thread="auth", text="s2")), digest=True),
                 item(msg(id="a3", seq=3, kind="ask", to=["bob"], text="q?"))]
        ctx, emitted, overflow = render.build(items, "bob", 2000, 600)
        self.assertEqual(ctx.split("\n")[1:], ["a3 alice→you ask: q?", "#auth: 2 new (a1..a2), last alice: s2"])
        lines = {it["msg"]["id"]: it["line"] for it in emitted}
        self.assertEqual(lines["a1"], lines["a2"])
        self.assertEqual(overflow, [])

    def test_digest_lines_are_capped(self):
        items = [dict(item(msg(id=f"a{i}", seq=i, thread=f"t{i}", text="x")), digest=True) for i in range(1, 11)]
        ctx, emitted, overflow = render.build(items, "bob", 2000, 600)
        self.assertEqual(sum(1 for line in ctx.split("\n") if line.startswith("#t")), render.DIGEST_MAX_LINES)
        self.assertEqual([it["msg"]["id"] for it in overflow], ["a9", "a10"])
        self.assertIn("not shown yet", ctx.split("\n")[-1])

    def test_forged_digest_input_stays_under_the_cap(self):
        items = [dict(item(msg(id="x" * 64 + str(i), seq=i, thread="t" * 64, text="漢" * 4000, **{"from": "f" * 500})),
                      digest=True, room=f"room{i}") for i in range(1, 30)]
        ctx, _, _ = render.build(items, "bob", 2000, 600)
        self.assertLessEqual(len(json.dumps(ctx, ensure_ascii=True)) - 2, render.MAX_CONTEXT_JSON)
        for line in ctx.split("\n"):
            self.assertNotIn("\n", line)
```

`tests/test_hook_deliver.py`:

```python
class DigestDeliverTest(DeliverCase):
    def setUp(self):
        super().setUp()
        rooms.set_prefs(self.b, "r", digest=True)

    def test_a_turn_costs_one_line_per_active_thread_and_asks_arrive_whole(self):
        post(self.a, "r", "s1", thread="auth")
        post(self.a, "r", "report", thread="auth", kind="done", to=["bob"])
        post(self.a, "r", "s2", thread="db")
        post(self.a, "r", "need you", thread="auth", kind="ask", to=["bob"])
        lines = self.context(self.deliver(self.b)).split("\n")[1:]
        self.assertEqual(lines, ["a4 alice→you ask #auth: need you",
                                 "#auth: 2 new (a1..a2), last alice: report",
                                 "#db: 1 new (a3), last alice: s2"])

    def test_addressed_prop_arrives_whole_in_digest_mode(self):
        post(self.a, "r", "ship at 3", kind="prop", to=["bob"])
        self.assertIn("a1 alice→you prop: ship at 3", self.context(self.deliver(self.b)))

    def test_digest_line_is_confirmed_from_the_transcript(self):
        path = os.path.join(self.tmp, "t.jsonl")
        open(path, "w").close()
        post(self.a, "r", "s1", thread="auth")
        post(self.a, "r", "s2", thread="auth")
        out = self.deliver(self.b, transcript_path=path)
        self.confirm(path, out)  # existing helper: writes the hook_additional_context record
        self.assertIsNone(self.deliver(self.b, transcript_path=path))

    def test_an_unconfirmed_digest_comes_back_once_as_a_digest(self):
        path = os.path.join(self.tmp, "t.jsonl")
        open(path, "w").close()
        post(self.a, "r", "s1", thread="auth")
        self.deliver(self.b, transcript_path=path)             # never confirmed
        again = self.context(self.deliver(self.b, transcript_path=path))
        self.assertIn("#auth: 1 new (a1), last alice: s1", again)
        self.assertIsNone(self.deliver(self.b, transcript_path=path))

    def test_forged_digest_value_means_off(self):
        rooms.set_prefs(self.b, "r", digest=None)
        with store.room_lock("r"):
            members = store.load_members("r")
            members[self.b]["digest"] = "yes"
            store.save_members("r", members)
        post(self.a, "r", "s1", thread="auth")
        self.assertIn("a1 alice→all say #auth: s1", self.context(self.deliver(self.b)))

    def test_a_held_message_is_not_counted_in_a_digest(self):
        sessions.update_meta(self.a, lambda meta: meta.update(permission_mode="bypassPermissions"))
        post(self.a, "r", "s1", thread="auth", mode="bypassPermissions")
        self.assertNotIn("#auth", self.context(self.deliver(self.b)))
```

(`confirm` is a staticmethod on `DeliverTest`, at `tests/test_hook_deliver.py:141`. Move it up to `DeliverCase` in this task so `DigestDeliverTest` can use it. The move is safe because it doesn't change `DeliverTest`'s behaviour.)

`tests/test_cli.py`:

```python
class DigestCliTest(CliCase):
    def setUp(self):
        super().setUp()
        self.run_cli(self.b, "join", "r", "--as", "bob")

    def test_digest_on_and_off(self):
        code, out, _ = self.run_cli(self.b, "digest", "on")
        self.assertEqual((code, out), (0, "digest on in r: one line per thread; asks, errs, props and naks "
                                          "addressed to you arrive whole\n"))
        self.assertIs(store.load_members("r")[self.b]["digest"], True)
        self.assertEqual(self.run_cli(self.b, "digest", "off")[1], "digest off in r\n")
        self.assertNotIn("digest", store.load_members("r")[self.b])
        self.assertEqual(self.run_cli(self.b, "digest", "maybe")[0], 2)
        self.assertEqual(self.run_cli(self.c, "digest", "on", "--room", "r")[0], 3)
```

`tests/test_cli_view.py`, `WhoTest`: after `digest on` by bob, bob's `who` line ends with `· digest`.

`tests/test_rooms.py`, `test_digest_survives_clear`: `set_prefs(b, "r", digest=True)`; after `carry_over(b, new)`, `load_members("r")[new]["digest"] is True`.

- [ ] **Step 2: Run them and see them fail.**
  Run: `python3 -m unittest tests.test_render tests.test_hook_deliver tests.test_cli tests.test_cli_view tests.test_rooms -q`

- [ ] **Step 3: Implement.**

`render.py`:

```python
DIGEST_GIST_CHARS = 60
DIGEST_MAX_LINES = 8
WHOLE_KINDS = ("ask", "err", "prop", "nak")


def whole(msg, me) -> bool:
    """In digest mode, whether msg still arrives as its own line: addressed to me by name and an
    ask, err, prop or nak, or posted with --wake (a prop's silence is consent once seen)."""
    return addressed_by_name(msg, me) and (msg.get("kind") in WHOLE_KINDS or msg.get("wake") is True)


def digest_line(group, multi) -> str:
    first, last = group[0], group[-1]
    thread = thread_of(first["msg"])
    label = f"#{thread}" if thread else "unthreaded"
    first_id = escape_text(_clip_field(first["msg"].get("id", "?")))
    last_id = escape_text(_clip_field(last["msg"].get("id", "?")))
    span = first_id if len(group) == 1 else f"{first_id}..{last_id}"
    line = (f"{label}: {len(group)} new ({span}), last {_resolved_name(last['msg'], last['members'])}: "
            f"{gist(last['msg'].get('text', ''), DIGEST_GIST_CHARS)}")
    if multi:
        line = f"[{escape_text(_clip_field(first.get('display') or first['room']))}] {line}"
    return line
```

In `build`:
- Split `ordered` into whole items and digest groups. Groups are keyed by `(it["room"], thread_of(it["msg"]))`.
  - **Sort each group's items by `msg["seq"]` before calling `digest_line`.** `ordered` is sorted by priority first, so an addressed `done` (priority 1) would otherwise come before an older broadcast (priority 2), and the span and "last" would be wrong.
  - Order the groups by `(0 if any item is redelivered else 1, the group's smallest seq)`.
- Pack the whole items exactly as today.
- Then, for each group in order:
  - if `DIGEST_MAX_LINES` groups were already emitted, or the group's line doesn't fit the budget or `pack_cap`, append all its items to `overflow`;
  - else append the line, and add `dict(it, line=line)` to `emitted` for every item in the group.
- Digest groups never use the first-line shrink. If no whole item was emitted and the first group doesn't fit (impossible under the field clips, since a digest line is at most about 330 characters), it overflows like any other item.
- The overflow and tail handling is unchanged.

`hook._Fire.deliver`:
- Set `item["digest"] = digest_on and not render.whole(msg, me)`, where `_, digest_on = self.prefs(room)`.
- This runs for redelivered and carried overflow items too, because they pass through `deliver`.
- Look-ahead only takes priority-0 items, which are always whole.

`cli`:
- `p = sub.add_parser("digest", help="one line per thread instead of every line")`, with `p.add_argument("state", choices=("on", "off"))` and `--room`, `needs_home=True`. A bad choice is an argparse exit 2.
- `cmd_digest` calls `_members_or_exit`, then `rooms.set_prefs(sid, room, digest=(args.state == "on") or None)`, and prints the lines from the test.
- `cmd_who`: `if digest: line += " · digest"`.

- [ ] **Step 4: Run them and see them pass.** Use the same command as Step 2.

- [ ] **Step 5: Update the docs.**
  - SKILL.md, under "Join and look around" (about 140 bytes): `` - `passnote digest on` (for a hub): one `#<thread>: N new` line per thread; asks, errs, props and naks to you still arrive whole. `passnote read --thread <name>` shows the lines. ``
  - README "How it works": add a paragraph on digest mode, with an example.
  - Spec §7: add a "Digest delivery (#26)" paragraph with the whole/digested split, the format, the cap and the transcript rule. §10 CLI table: `digest on|off`.
  - `CONTEXT.md`, under Delivery and trust: **Digest**: "A delivery mode in which a member gets one line per thread with new activity instead of every line; asks, errs, proposals and naks addressed to it still arrive whole. _Avoid_: summary mode."
  - CHANGELOG `### Added`: "Digest mode for a hub member: `passnote digest on` delivers one line per active thread; asks, errs, props and naks addressed to it still arrive whole (#26)."

- [ ] **Step 6: Run the full suite and lint, and measure the skill.** The total added since base must be at most 750 bytes. If it is over, shorten Task 5's and Task 6's bullets, not the safety rules.

- [ ] **Step 7: Commit.** Message: `Digest mode for a hub member (#26)`, plus the byte delta and the Co-Authored-By line.

**Acceptance:**
- With digest on, a turn with new activity costs one line per active thread, plus whole lines for addressed asks, errs, props and naks.
- Digested lines are confirmed from the transcript and are not redelivered whole.
- Settings survive `/clear`.

---

## Shared-file matrix (for the controller)

| File | T1 | T2 | T3 | T4 | T5 | T6 |
|---|---|---|---|---|---|---|
| `plugin/lib/passnote/wake.py` | x | | | | | |
| `plugin/lib/passnote/config.py` | | x | x | | | |
| `plugin/lib/passnote/render.py` | | x | x | x | x | x |
| `plugin/lib/passnote/hook.py` | | x | | x | x | x |
| `plugin/lib/passnote/store.py` | | | x | | x | |
| `plugin/lib/passnote/cli.py` | | | x | | x | x |
| `plugin/lib/passnote/sessions.py` | | | | x | | |
| `plugin/lib/passnote/fold.py` | | | | x | | |
| `plugin/lib/passnote/rooms.py` | | | | x | x | x |
| `tests/support.py` | | x | x | | | |
| `tests/test_render.py` | | x | x | x | x | x |
| `tests/test_hook_deliver.py` | | x | x | x | x | x |
| `tests/test_cli.py` | x | | x | | x | x |
| `tests/test_cli_view.py` | | | | | x | x |
| `tests/test_hook_lifecycle.py` | | | | x | x | |
| `tests/test_rooms.py` | | | | | x | x |
| `tests/test_store.py` | | | x | | x | |
| `tests/test_config.py` | | x | x | | | |
| `tests/test_wake.py` | x | | | | | |
| `tests/test_fold.py` | | | | x | | |
| `tests/integration/test_headless.py` | x | | | | | |
| `plugin/skills/passnote/SKILL.md` | x | | x | x | x | x |
| `README.md`, `CHANGELOG.md`, spec, `CONTEXT.md` | x | x | x | x | x | x |

The tasks are sequential on one branch, so no two run at once. The matrix is for checking a re-run or a reordering.

## Self-review notes

- **Coverage:**
  - #19's "Done when" is partly met (the receiver reads the text once). The one-tool-call half is deferred to Phase D, with the reason.
  - #20, #27, #28, #25 and #26 each have tests for every acceptance line.
- **Names used across tasks:**
  - `render.addressed_by_name` (T2; used by T5, T6);
  - `store.FULL_TEXT_ID_RE` (T3; used by T4);
  - `render.build(items, me, budget, clip, tail=None)` (T4) with item keys `clip` (T2) and `digest` (T6);
  - `store.member_prefs`, `rooms.set_prefs` and `_Fire.prefs` (T5; used by T6);
  - `render.thread_of` (T5; used by T6).
- **Not done here:** #29, #24, #14 and #5.
