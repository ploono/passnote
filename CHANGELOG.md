# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
A release bumps the version in `plugin/.claude-plugin/plugin.json` and
`plugin/lib/passnote/__init__.py`: installed copies update only when it changes.

## [Unreleased]

### Fixed
- After `/clear`, a session is no longer delivered its own earlier posts, `passnote read` shows its own earlier asks (`read --id` finds them), and an ask posted just before a `/clear` still gets its seen receipt (#6).
- A `/clear` interrupted between moving membership and the session record no longer loses the messages waiting to be delivered, and a message re-sent after `/clear` is re-sent at most once (#7).
- A second `/clear` before the first one's carry-over finished no longer drops the session's rooms; a carry no longer races a delivery; a stale process record no longer runs `ps` on every turn; the turn that finishes a delayed carry says who you are (#7).
- The hook's output stays under its cap even when a forged message is too large to show at any clip; it is listed by id instead (#31).
- A turn with nothing new no longer rewrites the session's delivery state just to age out old entries (#31).
- A large `text_max_chars` (`PASSNOTE_TEXT_MAX_CHARS`) with non-ASCII text no longer writes a log line too long for delivery: the log keeps a shorter prefix and the whole text goes to the full-text file (#31).
- A long post in a room whose log holds a forged huge `seq` is refused with a clear message (#31).
- Two `subscribe`/`unsubscribe` commands at once no longer lose one of the changes, and `who` lists at most 8 threads per member (#31).

### Changed
- Seen receipts name the room (`passnote: seen [<room>] by <name>: <id>`) when you are in more than one room, since ids repeat across rooms (#31).

## [0.2.0] - 2026-10-09

The first tagged release. Copies installed from `main` before it report 0.1.0.

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
