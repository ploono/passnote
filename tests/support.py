"""Shared test helpers: isolated PASSNOTE_HOME, clean env, import path, leak check."""
import gc
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
import warnings
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "plugin", "lib")
BIN = os.path.join(ROOT, "plugin", "bin", "passnote")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

CLEAR_ENV = (
    "PASSNOTE_HOME", "XDG_STATE_HOME", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID",
    "CLAUDE_PROJECT_DIR", "PASSNOTE_ALLOW_BYPASS", "PASSNOTE_INBOUND", "CLAUDE_CONFIG_DIR",
    "FORCE_PROMPT_CACHING_5M", "CLAUDE_CODE_PROMPT_CACHE_TTL", "ENABLE_PROMPT_CACHING_1H",
    "ANTHROPIC_API_KEY", "PASSNOTE_RENDER_BUDGET_CHARS", "PASSNOTE_CLIP_CHARS",
    "PASSNOTE_TEXT_MAX_CHARS", "PASSNOTE_TTL_SECONDS",
)


def new_sid():
    return str(uuid.uuid4())


class HomeCase(unittest.TestCase):
    """Each test gets a fresh PASSNOTE_HOME, a fake Claude config dir, a clean env, git that
    ignores the developer's config, and fails if it leaks a file or other resource."""

    def setUp(self):
        self._fail_on_leaked_resources()
        self.tmp = tempfile.mkdtemp(prefix="passnote-test-")
        os.chmod(self.tmp, 0o700)
        self.home = os.path.join(self.tmp, "home")
        self.claude_home = os.path.join(self.tmp, "claude")
        os.makedirs(self.claude_home)
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in CLEAR_ENV:
            os.environ.pop(key, None)
        os.environ["PASSNOTE_HOME"] = self.home
        os.environ["CLAUDE_CONFIG_DIR"] = self.claude_home
        # git (default_room, test repos) must not read the developer's ~/.gitconfig or system config.
        # GIT_CONFIG_GLOBAL needs git >= 2.32; older git reads $HOME/.gitconfig and
        # $XDG_CONFIG_HOME/git/config, so point those at empty temp dirs too.
        os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        os.environ["HOME"] = os.path.join(self.tmp, "user-home")
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "user-config")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)

    def _fail_on_leaked_resources(self):
        """-W error doesn't fail a test on a leaked file: CPython reports the ResourceWarning
        raised in __del__ through sys.unraisablehook ("Exception ignored ...") and moves on.
        Turn ResourceWarning into an error, collect what reaches the hook, and fail the test."""
        caught = []
        catcher = warnings.catch_warnings()
        catcher.__enter__()
        self.addCleanup(catcher.__exit__, None, None, None)
        warnings.simplefilter("error", ResourceWarning)
        previous = sys.unraisablehook
        sys.unraisablehook = lambda unraisable: caught.append(
            f"{unraisable.exc_type.__name__}: {unraisable.exc_value}")
        self.addCleanup(setattr, sys, "unraisablehook", previous)
        self.addCleanup(self._check_leaks, caught)

    def _check_leaks(self, caught):
        gc.collect()
        if caught:
            self.fail("leaked resources: " + "; ".join(caught))

    def env(self, sid, **extra):
        env = dict(os.environ)
        env["CLAUDE_CODE_SESSION_ID"] = sid
        env.update({key: str(value) for key, value in extra.items()})
        return env


def join(sid, room, name, mode="default", root="/tmp/root"):
    """Join `room` as `name` with a recorded permission mode, as the hook would record it."""
    from passnote import rooms, sessions
    rooms.join(sid, room, name, root, display=room)

    def record(meta):
        meta["permission_mode"] = mode
        meta["name_source"] = "as"

    sessions.update_meta(sid, record)


def post(sid, room, text, kind="say", to="all", mode="default", **extra):
    from passnote import store
    me = store.load_members(room)[sid]
    rec = {"from": me["name"], "sid": sid, "to": to, "kind": kind, "text": text, "mode": mode}
    rec.update(extra)
    return store.append_message(room, rec, me["alias"])


def hook_input(sid, event="PostToolBatch", mode="default", **extra):
    data = {"session_id": sid, "hook_event_name": event, "permission_mode": mode, "cwd": "/tmp"}
    data.update(extra)
    return json.dumps(data)


class CliCase(HomeCase):
    """Runs passnote.cli.main in-process. sid=None means a human terminal (no CLAUDE_CODE_SESSION_ID)."""

    def setUp(self):
        super().setUp()
        self.a, self.b, self.c = new_sid(), new_sid(), new_sid()

    def run_cli(self, sid, *argv, stdin="", **env_extra):
        import contextlib
        import io
        from passnote import cli
        if sid:
            env = self.env(sid, **env_extra)
        else:
            env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
            env.update({k: str(v) for k, v in env_extra.items()})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(list(argv), stdin=io.StringIO(stdin), stdout=out, stderr=err, env=env)
        return code, out.getvalue(), err.getvalue()
