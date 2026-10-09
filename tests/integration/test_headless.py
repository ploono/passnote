"""End-to-end with real Claude Code sessions. Opt-in: needs a logged-in `claude`.

Run: PASSNOTE_INTEGRATION=1 python3 -m unittest discover -s tests -p 'test_headless.py' -v

Each test runs a few short haiku sessions with the plugin loaded through --plugin-dir, no user or
project settings, no MCP servers and passnote's storage in a temp dir. The login is the only
thing they share with the developer's own sessions. PASSNOTE_IT_KEEP=1 keeps the temp dir (state,
errors.log, session outputs) for a look after a failure.

The tests drive a real model, so a test can fail (not skip) when the model doesn't follow the
prompt or the protocol, e.g. it never runs `passnote post`. Check the run*.out files before
suspecting passnote.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

from support import BIN, ROOT

PLUGIN = os.path.join(ROOT, "plugin")
# The parent session's identity and messaging socket: without them the children are independent
# sessions. Named one by one, not by the CLAUDE_CODE_ prefix, so login and provider settings
# (CLAUDE_CODE_OAUTH_TOKEN, CLAUDE_CODE_USE_BEDROCK, CLAUDE_CODE_USE_VERTEX, ...) still reach them.
PARENT_ENV = frozenset((
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_PID", "CLAUDE_PROJECT_DIR", "CLAUDECODE", "CLAUDE_EFFORT",
    # passnote reads it for warmth: the children's must not depend on the developer's value
    "CLAUDE_CODE_PROMPT_CACHE_TTL",
))
PARENT_PREFIXES = ("PASSNOTE_",)
ISOLATION = ["--model", "haiku", "--plugin-dir", PLUGIN, "--setting-sources", "", "--strict-mcp-config",
             "--mcp-config", '{"mcpServers":{}}']
SESSION_TIMEOUT = 300
DOORBELL_TIMEOUT = 180

# A PreToolUse hook for the sender in the doorbell test: SendMessage may only reach this test's
# receiver (argv[1]), never one of the developer's own sessions.
ONLY_TO_RECEIVER = r'''import json, re, sys
data = json.load(sys.stdin)
to = str((data.get("tool_input") or {}).get("to", ""))
if not re.fullmatch(re.escape(sys.argv[1]) + r"( \[[0-9a-f]+\])?", to):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                      "permissionDecisionReason": "integration test: SendMessage only to the test receiver"}}))
'''


class StreamSession:
    """A headless session kept open on stream-json stdin, so it sits idle between turns and can
    take a doorbell like an interactive one."""

    def __init__(self, cmd, cwd, env, stderr_path):
        self.stderr = open(stderr_path, "w")
        self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.stderr, text=True, bufsize=1)
        self.events = []
        self.lock = threading.Lock()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            with self.lock:
                self.events.append(event)

    def send(self, text):
        self.proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
        self.proc.stdin.flush()

    def results(self):
        with self.lock:
            return [event for event in self.events if event.get("type") == "result"]

    def wait_results(self, count, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.proc.poll() is None:
            if len(self.results()) >= count:
                return True
            time.sleep(1)
        return len(self.results()) >= count

    def assistant_text(self, after_results):
        """Text the model wrote after the first `after_results` turns ended."""
        texts, seen = [], 0
        with self.lock:
            events = list(self.events)
        for event in events:
            if event.get("type") == "result":
                seen += 1
            elif event.get("type") == "assistant" and seen >= after_results:
                for block in (event.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(block.get("text", ""))
        return "\n".join(texts)

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.reader.join(timeout=10)
        self.proc.stdout.close()
        self.stderr.close()


@unittest.skipUnless(os.environ.get("PASSNOTE_INTEGRATION") == "1", "set PASSNOTE_INTEGRATION=1 (needs a logged-in claude)")
class HeadlessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="passnote-it-")
        if os.environ.get("PASSNOTE_IT_KEEP") == "1":
            sys.stderr.write(f"\nkeeping {self.tmp}\n")
        else:
            self.addCleanup(shutil.rmtree, self.tmp, True)
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        self.env = {k: v for k, v in os.environ.items() if k not in PARENT_ENV and not k.startswith(PARENT_PREFIXES)}
        self.home = os.path.join(self.tmp, "state")
        self.env["PASSNOTE_HOME"] = self.home
        self.runs = 0

    def claude(self, prompt, resume=None, tools="Bash", extra=(), stream=False):
        """One `claude -p` turn: (session id, result text, stdout)."""
        fmt = ["--output-format", "stream-json", "--verbose"] if stream else ["--output-format", "json"]
        cmd = ["claude", "-p", prompt] + ISOLATION + fmt + ["--allowedTools", tools] + list(extra)
        if resume:
            cmd += ["--resume", resume]
        self.runs += 1
        res = subprocess.run(cmd, cwd=self.work, env=self.env, capture_output=True, text=True, timeout=SESSION_TIMEOUT)
        self.keep(f"run{self.runs}.out", res.stdout)
        self.assertEqual(res.returncode, 0, res.stderr[-2000:] + self.errors())
        if stream:
            final = [json.loads(line) for line in res.stdout.splitlines() if '"type":"result"' in line]
            data = final[-1] if final else {}
        else:
            data = json.loads(res.stdout)
        return data.get("session_id"), data.get("result", ""), res.stdout

    def keep(self, name, text):
        with open(os.path.join(self.tmp, name), "w") as fh:
            fh.write(text)

    def errors(self):
        try:
            with open(os.path.join(self.home, "errors.log")) as fh:
                return "\nerrors.log:\n" + fh.read()[-2000:]
        except OSError:
            return ""

    def room_file(self, room, name):
        try:
            with open(os.path.join(self.home, "rooms", room, name)) as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except OSError:
            return []

    def stream_session(self, name, extra=()):
        session = StreamSession(
            ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
            + ISOLATION + ["--allowedTools", "Bash"] + list(extra),
            self.work, self.env, os.path.join(self.tmp, f"{name}.err"))
        self.addCleanup(session.close)
        return session

    def cli_as(self, sid, *args, stdin=""):
        """The CLI as that session's own Bash tool runs it (same session id), without a model turn."""
        res = subprocess.run([BIN] + list(args), input=stdin, cwd=self.work, capture_output=True, text=True,
                             env=dict(self.env, CLAUDE_CODE_SESSION_ID=sid), timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr + self.errors())
        return res.stdout

    def test_message_reaches_a_resumed_session_through_the_hook(self):
        # Bob's process has ended before alice joins and posts; `--resume` keeps his session id (spec §5).
        bob, _, _ = self.claude(f'Run exactly this shell command and nothing else, then stop: "{BIN}" join it --as bob')
        self.claude(f'Run exactly this shell command and nothing else, then stop: "{BIN}" join it --as alice '
                    f"&& printf 'the codeword is PERIWINKLE' | \"{BIN}\" post --to bob")
        with open(os.path.join(self.home, "rooms", "it", "members.json")) as fh:
            self.assertIn(bob, json.load(fh), "bob's membership did not outlast his session's process")
        sid, answer, _ = self.claude("If passnote delivered any messages to you, repeat the codeword they contain. "
                                     "Otherwise answer NONE.", resume=bob)
        self.assertEqual(sid, bob)
        self.assertIn("PERIWINKLE", answer, self.errors())

    def test_message_reaches_a_running_session_through_the_hook_and_after_clear(self):
        ask = "If passnote delivered any messages to you, repeat the codeword they contain. Otherwise answer NONE."
        bob = self.stream_session("bob")
        bob.send(f'Run exactly this shell command and nothing else, then stop: "{BIN}" join it --as bob')
        self.assertTrue(bob.wait_results(1, SESSION_TIMEOUT), "bob never finished joining")
        alice, _, _ = self.claude(f'Run exactly this shell command and nothing else, then stop: "{BIN}" join it '
                                  f"--as alice && printf 'the codeword is PERIWINKLE' | \"{BIN}\" post --to bob")
        bob.send(ask)  # UserPromptSubmit delivers alice's post
        self.assertTrue(bob.wait_results(2, SESSION_TIMEOUT), "bob never answered")
        self.assertIn("PERIWINKLE", bob.assistant_text(after_results=1), self.errors())

        # /clear gives bob a new session id; SessionEnd(clear) and SessionStart(clear) carry the membership.
        bob.send("/clear")
        self.assertTrue(bob.wait_results(3, SESSION_TIMEOUT), "/clear never finished")
        self.cli_as(alice, "post", "--to", "bob", stdin="the codeword is now SAFFRON")
        bob.send(ask)
        self.assertTrue(bob.wait_results(4, SESSION_TIMEOUT), "bob never answered after /clear")
        self.assertIn("SAFFRON", bob.assistant_text(after_results=3), self.errors())
        self.assertEqual([ev["type"] for ev in self.room_file("it", "events.jsonl") if ev["type"] == "carry"],
                         ["carry"])

    def test_doorbell_wakes_an_idle_receiver_that_accepts_cross_session_messages(self):
        rx = "pn-it-" + uuid.uuid4().hex[:8]  # the session name SendMessage addresses, and the member name
        receiver = self.stream_session("receiver", [
            "--name", rx, "--settings", json.dumps({"crossSessionInbound": "accept"}),
            "--append-system-prompt", "When a message from another Claude session arrives, reply only with GOT: "
            "followed by any codeword passnote delivered to you. Never use tools for it."])
        receiver.send(f'Run exactly this shell command and nothing else, then stop: "{BIN}" join it --as {rx}')
        self.assertTrue(receiver.wait_results(1, SESSION_TIMEOUT), "the receiver never finished its first turn")

        guard = os.path.join(self.tmp, "only_to_receiver.py")
        self.keep("only_to_receiver.py", ONLY_TO_RECEIVER)
        sender_settings = {"hooks": {"PreToolUse": [{"matcher": "SendMessage", "hooks": [
            {"type": "command", "command": f"python3 {shlex.quote(guard)} {rx}", "timeout": 10}]}]}}
        # Alice joins in one turn and posts in a later one: her permission mode is recorded by the
        # UserPromptSubmit hook, and `post` holds (no doorbell) while the sender's mode is unknown.
        alice, _, _ = self.claude(f'Run exactly this shell command and nothing else, then stop: "{BIN}" join it --as alice')
        _, _, out = self.claude(
            f'Run exactly this shell command and nothing else: printf \'the codeword is MARIGOLD\' | "{BIN}" post --kind ask --to {rx}\n'
            "If its output has a line starting with WAKE, make exactly the SendMessage call that line shows. Then stop.",
            resume=alice, tools="Bash,SendMessage", extra=["--settings", json.dumps(sender_settings)], stream=True)

        wakes = [ev for ev in self.room_file("it", "events.jsonl") if ev.get("type") == "wake"]
        self.assertEqual([(ev["to"], ev["decision"]) for ev in wakes], [(rx, "WAKE")], self.errors())
        self.assertIn('"name":"SendMessage"', out.replace(" ", ""), "the sender never called SendMessage")
        self.assertTrue(receiver.wait_results(2, DOORBELL_TIMEOUT), "the doorbell did not wake the receiver")
        # The doorbell carries no text (#19), so the text reaches the receiver only through the hook:
        # the proof is a UserPromptSubmit attachment in its transcript, after the doorbell.
        with open(os.path.join(self.home, "rooms", "it", "members.json")) as fh:
            receiver_sid = next((sid for sid, info in json.load(fh).items() if info.get("name") == rx), None)
        self.assertTrue(receiver_sid, "receiver not in members.json")
        with open(os.path.join(self.home, "sessions", receiver_sid, "meta.json")) as fh:
            transcript = json.load(fh).get("transcript_path")
        self.assertTrue(transcript and os.path.isfile(transcript), "the receiver's transcript path was not recorded")
        with open(transcript) as fh:
            records = [json.loads(line) for line in fh if line.strip().startswith("{")]
        bell = [i for i, rec in enumerate(records) if "from alice: passnote note waiting" in json.dumps(rec)]
        self.assertTrue(bell, "no doorbell in the receiver's transcript")
        delivered = [rec for rec in records[bell[0]:] if (rec.get("attachment") or {}).get("type") == "hook_additional_context"
                     and rec["attachment"].get("hookEvent") == "UserPromptSubmit"
                     and "MARIGOLD" in json.dumps(rec["attachment"].get("content"))]
        self.assertTrue(delivered, "the hook did not deliver MARIGOLD on the doorbell-woken turn")
        self.assertIn("MARIGOLD", receiver.assistant_text(after_results=1), self.errors())

    def test_a_subagent_cannot_post_as_its_joined_parent(self):
        post = f"printf SUBAGENT-NOTE | {shlex.quote(BIN)} post"
        _, _, out = self.claude(
            f"First run exactly this shell command and nothing else: {shlex.quote(BIN)} join it --as carol\n"
            "Then use the Agent tool (subagent_type general-purpose) with this task, word for word: "
            f"'Run exactly this shell command once, do not retry or work around it, and report what happened: {post}'\n"
            "Then stop.",
            tools="Bash,Agent", stream=True)
        posted = [msg for msg in self.room_file("it", "log.jsonl") if "SUBAGENT-NOTE" in str(msg.get("text"))]
        self.assertEqual(posted, [], "the subagent's post reached the room")
        # Positive evidence that the PreToolUse hook denied it (not that the model never tried).
        self.assertIn("a subagent can't post or change membership", out, self.errors())


if __name__ == "__main__":
    unittest.main()
