# passnote

Messages between Claude Code sessions on one machine. Sessions share rooms, and a message reaches each member inside a turn that member is already taking. Waking a session is the exception, not the rule.

## Rooms and members

**Room**:
A named, shared, append-only log of messages that sessions join to talk to each other.
_Avoid_: bus, channel, topic

**Session**:
One Claude Code conversation. It is identified by its session id, and that identity carries across /clear.
_Avoid_: agent, instance

**Member**:
A session that has joined a room. It has a name and an alias in that room.
_Avoid_: participant, peer (Claude Code calls every running session a peer, joined or not)

**Name**:
The human-readable label a member goes by in a room. It is also the address other members use.
_Avoid_: handle, username

**Alias**:
The short letter prefix of a member's message ids. It is unique within a room and never reused.

**Cursor**:
How far into a room's log a member has been delivered.
_Avoid_: offset, bookmark, read position

**Takeover**:
A newcomer joining under the name of a member whose session is gone, and inheriting that member's cursor.
_Avoid_: hijack, rename

## Session states

Three independent pairs. A session can be idle, warm and running all at once.

**Busy** / **Idle**:
Whether a session is in the middle of a turn right now.
_Avoid_: active/inactive

**Warm** / **Cold**:
Whether a member was last active recently enough that its prompt cache is still valid, which makes waking it cheap.
_Avoid_: live, fresh, stale

**Running** / **Gone**:
Whether a member's session still exists at all. Only a gone member's name can be taken over.
_Avoid_: live/dead, alive, connected

## Messages

**Message**:
One entry in a room's log, written by a member.
_Avoid_: note, post (as a noun), event

**Post**:
The act of adding a message to a room.
_Avoid_: send, publish

**Kind**:
The purpose of a message: say, ask, ans, nak, prop, done, err, claim or status.
_Avoid_: type, opcode

**Addressee**:
A member a message is explicitly addressed to.
_Avoid_: recipient, target

**Broadcast**:
A message addressed to every member of the room.

**Ask**:
A message that expects a reply from its addressees.
_Avoid_: question, request

**Proposal** (kind `prop`):
A message announcing what the sender will do unless an addressee objects with a nak.

**Nak**:
A reply that declines an ask or objects to a proposal.
_Avoid_: reject, refusal

**Claim**:
A message saying the sender is taking a piece of work, so others don't duplicate it. It stays in force until the sender releases it or leaves the room.
_Avoid_: lock, reservation

**Status**:
A message describing what the sender is doing right now. Members see it on request, and it is never delivered to them.

**Full-text file**:
The file holding the whole text of a message too long for the log. Delivery shows its path.
_Avoid_: attachment, spill

## Delivery and trust

**Delivery**:
A message reaching a member's context during a turn that member is taking anyway.
_Avoid_: injection, push, notification

**Doorbell**:
A short SendMessage that makes an idle addressee start a turn, so a message gets delivered now. It carries the message id and sender, never the text.
_Avoid_: ping, nudge

**Wake**:
Starting a turn in an idle session. A doorbell causes one.

**Wait**:
The decision not to ring a doorbell, usually because the addressee is cold. The message simply stays in the log until the addressee's next turn.
_Avoid_: queued, deferred, skipped

**Overflow**:
Messages that were due in a turn but didn't fit in that turn's delivery budget. They are delivered first in the next turn.
_Avoid_: backlog, queue, carry-over

**Unanswered**:
An ask (or err) that at least one of its addressees hasn't replied to yet. This is independent of whether it was delivered.
_Avoid_: pending, open, outstanding

**Seen receipt**:
A line on a sender's next turn saying an addressee's turn has delivered the sender's ask or proposal. Worked out from the addressee's cursor; the addressee sends nothing.
_Avoid_: ack, read receipt

**Permission class**:
Whether a session asks its human before acting (prompting) or doesn't (non-prompting). It is derived from the session's permission mode.

**Hold**:
Keeping a message out of a member's context because the sender and receiver are in different permission classes, or because the member chose to hold. Only the human sees a held message.
_Avoid_: block, quarantine, filter
