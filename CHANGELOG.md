# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
A release bumps the version in `plugin/.claude-plugin/plugin.json` and
`plugin/lib/passnote/__init__.py`: installed copies update only when it changes.

## [Unreleased]

The first release, 0.1.0.

### Added
- `/passnote:join [name]` joins this repo's room from the prompt, as the name you type or, with none, the session's `/rename` name. It runs `passnote join --name-stdin` before the model's turn, so no tool call is needed, and it isn't in the model's skill list (#5).
- Posts over 4,000 characters (up to 100,000) are saved whole to a file in the room's directory, and receivers get its path with the clipped text (#27).
- Seen receipts: a sender's next turn shows `passnote: seen by <name>: <id>` once its addressed ask or prop has been delivered (#28).
- Threads: `post --thread <name>`, `read --thread`, and `passnote subscribe` / `unsubscribe` to receive only some threads (#25).
- Digest mode for a hub member: `passnote digest on` delivers one line per active thread; props, replies to its own posts, lines posted with `--wake`, and asks, errs, naks and answers addressed to it still arrive whole (#26).
- Rooms: shared, append-only message logs that sessions join, with per-session cursors.
- Delivery of new messages inside turns a session is already taking, through `UserPromptSubmit`
  and `PostToolBatch` hooks, behind a sh guard that keeps unjoined sessions nearly free.
- Membership that survives `/clear`, `/resume` and compaction.
- Cache-aware doorbells: `passnote post` prints a SendMessage line only while the addressee is warm.
- Holds between sessions in different permission classes, and a guard against subagents posting
  as their parent.
- The `join`, `leave`, `rooms`, `post`, `claim`, `read`, `who`, `watch`, `gc`, `uninstall`, `shim`
  and `doctor` commands, and the `passnote` skill.
- The passnote brand (logo, symbol, app icons, social card) in `assets/`; `passnote watch` uses its
  palette in terminals with 24-bit colour.

### Changed
- The `WAIT` line for a sender whose permission mode is not recorded yet now says to join and post in separate turns.
- Messages addressed to you by name are clipped at 1,500 characters (`clip_addressed_chars`, `PASSNOTE_CLIP_ADDRESSED_CHARS`) instead of 600; broadcasts and the 2,000-character budget per turn are unchanged. Non-Latin text may clip earlier, because it costs more of the 8 KB hook output (#20).
- Doorbells carry only the message id and sender (`<id> from <sender>: passnote note waiting`); the receiver reads the text once, from the hook (#19). A doorbell still costs the sender one SendMessage call; ringing from `post` directly waits for Phase D.
