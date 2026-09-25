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
def _no_ambient_github_api_root(monkeypatch):
    """The GitHub backend honors GITHUB_API_URL / GITHUB_UPLOADS_URL (GHE, test mocks). A CI
    runner exports its own server's API root there — on GitHub that is https://api.github.com,
    so the suite never noticed; a Forgejo runner exports e.g. http://10.89.0.1:3000/api/v1, which
    the backend (correctly) refuses to send a token to, failing every GitHub-backend test. Tests
    that need an override set it themselves.
    """
    monkeypatch.delenv("GITHUB_API_URL", raising=False)
    monkeypatch.delenv("GITHUB_UPLOADS_URL", raising=False)


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
