"""Shared fixtures — a FAKE in-memory backend so tests never hit live GitHub/Linear."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Make the package importable when running pytest from the repo root without an install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Files and directories the tests create must not be group-writable: the config loader skips an
# ancestor rig.yaml (or its directory) that others can write (tasklib/config.py
# `_trusted_ancestor`). The suite assumed a 022 umask (GitHub-hosted runners, macOS); a Forgejo /
# self-hosted runner user with Ubuntu's user-private-group umask 002 made every ancestor-cascade
# test read an empty layer. Pin it for the whole run (subprocesses inherit it).
os.umask(0o022)

from tasklib.backends import TicketBackend  # noqa: E402
from tasklib.config import ATTACHMENT_MODE_ENV_VAR  # noqa: E402
from tasklib.model import State, Ticket  # noqa: E402
from tasklib.render import parse, render  # noqa: E402


@pytest.fixture(autouse=True)
def _no_ambient_attachment_mode_override(monkeypatch):
    """review finding: TASK_CLI_ATTACHMENT_MODE (config.py's highest-precedence config layer)
    is documented as something a developer exports in their own shell profile — the EXACT
    documented usage would otherwise silently flip every `attachment_mode`-asserting test in
    this suite for anyone who's actually set it up, since only some tests opt into the
    `isolated_state` fixture and none of them scrub this specific var. Autouse (unlike
    `isolated_state`) so it protects every test file, not just ones that ask for it.
    """
    monkeypatch.delenv(ATTACHMENT_MODE_ENV_VAR, raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_github_env_override(monkeypatch):
    """CI (GitHub/Forgejo Actions-compatible runners) ambiently injects GITHUB_API_URL (and,
    on a machine with `gh auth login` already run for a human operator, GITHUB_TOKEN/GH_TOKEN)
    into every job's environment -- a real, standard var (see github_issues.py's _api_root()
    docstring), not something any test here sets on purpose. Only the handful of tests that
    exercise _api_root()'s own override behavior `monkeypatch.setenv` it explicitly; every other
    test that merely constructs a GitHubIssuesBackend (indirectly calling _api_root()/
    _uploads_api_root()/credential resolution) inherited whatever the real CI host's ambient
    values were, which -- on the current Forgejo Actions runner -- point at the internal Forgejo
    instance over http, tripping the non-loopback-https guard and turning >15 unrelated tests
    into BackendError/CredentialError failures. Autouse (unlike `isolated_state`) so it protects
    every test file, not just ones that ask for it.
    """
    for var in ("GITHUB_API_URL", "GITHUB_UPLOADS_URL", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _no_real_fj_key_store(tmp_path, monkeypatch):
    """On Windows the fj key store lives under %APPDATA% (credentials._fj_key_files checks it
    first), and tests that only redirect HOME would otherwise read -- and, for an expired OAuth
    login, run the real `fj` to refresh -- the developer's REAL Forgejo login. Point APPDATA at
    an empty throwaway dir for every test; harmless where APPDATA is unused.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata-isolated"))


@pytest.fixture(autouse=True)
def _sane_umask():
    """config.py's `_trusted_ancestor` (UB-25 security hardening) legitimately fails-closed on
    an ancestor directory that is group/world-writable -- a real protection against another
    local user planting a `rig.yaml` that retargets this user's tickets/credentials. But every
    test here builds its throwaway ancestor tree with a bare `Path.mkdir(parents=True)`, whose
    actual on-disk mode is `0o777 & ~umask` -- fine under a normal 022 umask (the assumed
    default), but on a host whose ambient process umask is permissive (observed on the current
    CI runner), those test-created directories land group/world-writable and the exact same
    real security check then (correctly, per its own contract) skips every one of them, making
    the whole ancestor-config cascade silently no-op. Force a conventional umask for the
    duration of the suite so directory permissions are deterministic across hosts -- this is a
    test-hermeticity fix, not a change to the production trust check, which must stay
    fail-closed.
    """
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


@pytest.fixture(autouse=True)
def _no_real_classify_server(monkeypatch):
    """task-cli#245: `LayaClassifier.classify` talks to (and lazily SPAWNS) a machine-wide warm
    classify server by default. No test may reach a real one -- that would load the real model
    in a detached process that outlives the suite. `tests/test_classify_server.py` drives the
    server functions directly on throwaway sockets; tests of the client wiring re-enable it
    per-test with their own monkeypatches. Likewise the laya load never consults the REAL
    HuggingFace cache (`_cached_laya_snapshot`), so a developer machine that has the checkpoint
    cached sees the same `laya.load(<hub id>)` call as CI.
    """
    monkeypatch.setenv("TASK_CLASSIFY_SERVER", "0")
    monkeypatch.setattr("tasklib.classifiers._cached_laya_snapshot", lambda model_id: None)


@pytest.fixture(autouse=True)
def _no_real_laya_autoinstall(monkeypatch):
    """task-cli#208 review finding: `laya` is now the zero-config DEFAULT `classify.fallbacks`
    head (`config.DEFAULTS`), and `LayaClassifier.available()` auto-installs the real package
    (a real `pip`/network subprocess, `tasklib/laya_autoinstall.py`) the first time it finds
    the package genuinely not importable. Today every `classify` test in this suite stays
    hermetic only INCIDENTALLY -- because this repo's own committed `task.yaml` pins an
    explicit `classify.fallbacks` (no `laya` entry), and pytest's cwd is always this repo's
    root. That is an accident of THIS repo's config, not a guarantee any future test can rely
    on (a test that runs from a `task.yaml`-less `tmp_path` cwd, or one that legitimately
    clears the pin, would otherwise hit a REAL pip subprocess).

    Guards BOTH directions: `ensure_installed` is forced `False` (never a real pip/network
    call), and `importlib.util.find_spec("laya")` is forced `None` regardless of whether
    `laya` genuinely happens to be importable in the interpreter running this suite (e.g. a
    developer who followed the README's `uv sync --extra laya`, or reused the venv this
    ticket's own live end-to-end verification installed `laya` into) -- without this second
    guard, an unmocked classify test would silently route through a REAL loaded local model
    (a ~400M-param HuggingFace download) instead of the expected review-cli mock, on THAT
    specific machine only, the exact "passes here, breaks on your machine" failure mode this
    fixture exists to rule out entirely. Every OTHER `find_spec` lookup (any name but `laya`)
    passes straight through to the real implementation, unaffected.

    Autouse (unlike `isolated_state`) so it protects every test file, not just ones that ask
    for it -- mirrors `_no_ambient_attachment_mode_override` above. `tests/test_laya_
    autoinstall.py` (which tests `ensure_installed` ITSELF, with `subprocess`/`install` mocked
    at a lower level) overrides this fixture locally to a no-op; `tests/test_classifiers.py`'s
    own auto-install wiring tests explicitly re-monkeypatch `ensure_installed`/`find_spec`
    per-test, which still wins (same `monkeypatch` instance, applied after this fixture) over
    this default.
    """
    monkeypatch.setattr("tasklib.laya_autoinstall.ensure_installed", lambda **_kw: False)

    import importlib.util as _importlib_util

    real_find_spec = _importlib_util.find_spec

    def _find_spec_never_finds_laya(name, *args, **kwargs):
        if name == "laya":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(_importlib_util, "find_spec", _find_spec_never_finds_laya)


@pytest.fixture(autouse=True)
def _no_real_tg_notifier(monkeypatch):
    """task-cli#239: every ticket mutation calls `daemon.notify` with the configured notifier,
    whose zero-config default is the REAL `tg --tag report` (tg-cli) — so a plain full-suite
    run posted a fake "[task created] #1: ..." to the owner's Telegram report channel for every
    mutating test. A `tg` notifier (bare name or any path ending in `tg`) is recorded here and
    never spawned; any other notifier argv passes through to the real `notify`, so the tests of
    `notify` itself keep exercising its subprocess handling against harmless stand-ins
    (`true`/`false`/a missing binary). Returns the recorded `(message, notifier)` calls.

    Autouse so no test can leak by forgetting to opt in; a test that captures `notify` itself
    (`monkeypatch.setattr(daemon, "notify", ...)`) still wins, being applied after this.
    """
    from tasklib import daemon

    real_notify = daemon.notify
    sent: list[tuple[str, tuple[str, ...]]] = []

    def _notify_without_real_tg(message, notifier):
        if notifier and Path(notifier[0]).name == "tg":
            sent.append((message, tuple(notifier)))
            return True
        return real_notify(message, notifier)

    monkeypatch.setattr(daemon, "notify", _notify_without_real_tg)
    return sent


class FakeBackend:
    """An in-memory ``TicketBackend`` — round-trips through render/parse like a real one.

    Implements the full protocol so it is a structural drop-in. ``created`` records the
    sequence of created tickets for assertions; ``comments`` records appended comments.
    """

    name = "fake"

    def __init__(self) -> None:
        self._store: dict[str, Ticket] = {}
        self._next = 1
        self.comments: list[tuple[str, str]] = []
        self.attachments: list[tuple[str, str]] = []
        # None = "this test never set an identity" (mirrors a real backend's current_user()
        # returning None when it can't determine one) — set explicitly in tests that exercise
        # the reporter-scoped stale-nudge filter (issue #59, review P2).
        self.current_user_result: str | None = None

    def create(self, ticket: Ticket) -> Ticket:
        tid = f"#{self._next}"
        self._next += 1
        # round-trip through the body to mirror a real backend (body is the source of truth)
        stored = parse(render(ticket), ticket)
        stored.id = tid
        stored.url = f"https://fake/{tid.lstrip('#')}"
        stored.labels = list(ticket.labels)
        stored.state = ticket.state
        stored.created_at = ticket.created_at
        stored.closed_at = ticket.closed_at
        self._store[tid] = stored
        return stored

    def get(self, ticket_id: str) -> Ticket:
        if ticket_id not in self._store:
            from tasklib.backends import BackendError

            raise BackendError(f"fake: no ticket {ticket_id}")
        return self._store[ticket_id]

    def update(self, ticket: Ticket) -> Ticket:
        stored = parse(render(ticket), ticket)
        stored.id = ticket.id
        stored.url = f"https://fake/{ticket.id.lstrip('#')}"
        stored.labels = list(ticket.labels)
        stored.state = ticket.state
        stored.created_at = ticket.created_at
        stored.closed_at = ticket.closed_at
        self._store[ticket.id] = stored
        return stored

    def list(self, *, labels=None, state=None, limit=30) -> list[Ticket]:
        out = list(self._store.values())
        if labels:
            wanted = set(labels)
            out = [t for t in out if wanted & set(t.labels)]
        if state is not None:
            out = [t for t in out if t.state == state]
        return out[:limit]

    def search(self, query: str, *, state=None, limit=30) -> list[Ticket]:
        q = query.lower()
        out = [t for t in self._store.values() if q in t.title.lower() or q in render(t).lower()]
        if state is not None:
            out = [t for t in out if t.state == state]
        return out[:limit]

    def comment(self, ticket_id: str, body: str) -> None:
        self.get(ticket_id)  # raises if missing
        self.comments.append((ticket_id, body))

    def attach(self, ticket_id: str, file_path: str) -> str:
        self.attachments.append((ticket_id, file_path))
        return file_path

    def transition(self, ticket_id: str, state: State) -> Ticket:
        ticket = self.get(ticket_id)
        ticket.state = state
        return self.update(ticket)

    def session_tickets(self, session_label: str, *, limit=30) -> list[Ticket]:
        return self.list(labels=[session_label], limit=limit)

    def current_user(self) -> str | None:
        return self.current_user_result


@pytest.fixture
def fake_backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Redirect the sidecar + config dirs into tmp so tests never touch the real HOME."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    # neutralize session detection ambient noise unless a test sets it
    monkeypatch.delenv("TASK_SESSION", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    # Hermetic repo context (UB-25): run every command from an owned throwaway git repo with a
    # deterministic GitHub origin, never the developer's own checkout — whose origin (a Forgejo
    # host here) the `github.repo: auto` default would otherwise try to resolve before the fake
    # backend is ever reached. Tests about repo-vs-non-repo behavior pass their own `-C` or
    # `chdir`, which still wins.
    repo = tmp_path / "_hermetic_cwd_repo"
    repo.mkdir()
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/acme/widgets"],
        check=True,
    )
    monkeypatch.chdir(repo)
    return tmp_path


def assert_protocol(backend) -> None:
    """A FakeBackend must satisfy the runtime-checkable protocol."""
    assert isinstance(backend, TicketBackend)
