# Rename QUEUED → WAIT, backlog → overflow, pending → unanswered

Status: ready-for-agent
Type: task

## What

Decided in the domain-modeling session (2026-09-27). See `CONTEXT.md` for **Wait**, **Overflow** and **Unanswered**. The plan and spec use older words that blur three different states:

| Old (spec/plan) | New | Where it appears |
|---|---|---|
| `QUEUED <name> …` (post output, wake events) | `WAIT <name> …` | `cli._post` output, `wake.decide` return value, `events.jsonl` `decision`, `watch` event text, spec §8 output contract, skill text |
| backlog (emit state, `render.build` overflow list) | overflow | `sessions.load_emit/save_emit` key `backlog` → `overflow`, hook `_pending_refs`, plan Task 12 test names |
| pending (fold result, `who`, SessionStart reminder) | unanswered | `fold.fold()` key `pending` → `unanswered`, `who` line prefix `pending` → `unanswered`, reminder text "Waiting on your reply" (keep this phrasing) |

Keep the rendered overflow line as-is (`… N not shown yet: …`). It already reads well to the model.

## Why now

Implementation is at Task 1 of 18. Renaming before Tasks 5, 9, 10, 12, 14 and 15 land costs nothing; renaming afterwards touches tests and the skill's output contract.

## Tests

The existing plan tests change only by name and string. For example, `test_ask_queued_when_cold_and_wake_when_warm` expects `WAIT bob cold (last active …; --urgent to force)`. Combine this with ticket 01's "last active" wording.

## Comments
