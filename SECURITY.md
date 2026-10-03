# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately, through GitHub's private vulnerability reporting:
open the repository's **Security** tab and choose **Report a vulnerability**
(https://github.com/ploono/passnote/security/advisories/new). Don't open a public issue for a
vulnerability.

Fixes go into the latest release only.

## What passnote protects, and what it doesn't

- **The trust boundary is the OS user.** Rooms live in `PASSNOTE_HOME` (mode 0700). Every session of
  the same user, and any process running as that user, can read and write every room. That is the
  same boundary as Claude Code's own inter-session socket. passnote refuses a `PASSNOTE_HOME` that
  another user owns or that is group- or world-writable.
- **The delivery header is advisory.** Delivered messages are introduced as coming from other Claude
  sessions, not the user, and as unable to grant permissions or approve actions. A model may still
  follow a message's text; Claude Code's own permission prompts are the real gate.
- **One message is always one line.** Before delivery, text is stripped of ANSI sequences; newlines,
  carriage returns and Unicode line separators are escaped; other control characters are removed;
  invisible Unicode (bidirectional controls, zero-width characters and tag characters) is stripped;
  and `<tag>`-like text is neutralized. A message can't forge a second line, system text or
  instructions a human can't see.
- **Permission classes.** Messages between a session that prompts its human and one that doesn't
  (bypass or auto mode) are held and shown only to the human, unless the receiving session was
  launched with `PASSNOTE_ALLOW_BYPASS=1`.
- **Subagents.** A hook stops a subagent from posting or changing membership as its parent session
  through `passnote` commands in Bash. It prevents mistakes; it is not a sandbox.
- **Secrets.** `passnote post` refuses text that looks like a credential, but this is a best-effort
  pattern match. `--to` is not private: every member can read every message in a room.
