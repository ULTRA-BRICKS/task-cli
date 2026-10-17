"""Reuse a running laya server of ANY task-cli version (task-cli#282).

The socket is named after the task-cli version (`laya-<version>.sock`), so right after an
upgrade the server the previous version started is still running under its old name. Spawning
a current-version server next to it would load a second ~2 GB model; `start` and `predict`
must find and reuse the old one instead. Fake servers are `serve()` threads with canned
predictors (no model) on a short /tmp path (AF_UNIX paths are capped near 104 bytes on macOS).
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

import tasklib.classify_server as server
from tasklib.cli import main

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs AF_UNIX")


@pytest.fixture
def state_dir(monkeypatch):
    """A private state dir whose current-version socket is `laya-0.23.0.sock`."""
    monkeypatch.delenv("TASK_CLASSIFY_SERVER")
    d = Path(tempfile.mkdtemp(prefix="tcsr-", dir="/tmp"))
    os.chmod(d, 0o700)
    monkeypatch.setattr(server, "socket_path", lambda: d / "laya-0.23.0.sock")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def serving():
    """Start fake servers on demand; shut them all down afterwards."""
    started: list[tuple[threading.Thread, Path]] = []

    def _serve(path: Path, tag: str) -> Path:
        t = threading.Thread(
            target=server.serve, args=(path, lambda m, q: {tag: m}), kwargs={"idle_s": 30},
            daemon=True,
        )
        t.start()
        deadline = time.monotonic() + 5
        while True:
            try:
                server._probe(path)
                break
            except OSError:
                assert time.monotonic() < deadline, f"{path} never listened"
                time.sleep(0.01)
        started.append((t, path))
        return path

    yield _serve
    for t, path in started:
        try:
            server.request(path, None, None, timeout=5, op="shutdown")
        except OSError:
            pass
        t.join(5)


def _no_spawn(path):
    raise AssertionError(f"must not spawn a server at {path}: one is already running")


def _stale_socket(path: Path) -> Path:
    """A socket file nothing listens on (a server killed without cleanup)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.close()
    return path


def test_start_reuses_a_server_of_an_older_version(capsys, monkeypatch, state_dir, serving):
    old = serving(state_dir / "laya-0.22.0.sock", "old")
    monkeypatch.setattr(server, "_spawn", _no_spawn)
    rc = main(["classify-server", "start"])
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert out == f"{old}\n"


def test_predict_sends_to_a_server_of_an_older_version(monkeypatch, state_dir, serving):
    serving(state_dir / "laya-0.22.0.sock", "old")
    monkeypatch.setattr(server, "_spawn", _no_spawn)
    assert server.predict("hello", {}) == {"old": "hello"}


def test_the_highest_running_version_is_reused_first(monkeypatch, state_dir, serving):
    # Numeric, not lexical: "0.9.0" sorts after "0.22.0" as text.
    serving(state_dir / "laya-0.9.0.sock", "v09")
    newest = serving(state_dir / "laya-0.22.0.sock", "v22")
    monkeypatch.setattr(server, "_spawn", _no_spawn)
    assert server.start() == newest
    assert server.predict("x", {}) == {"v22": "x"}


def test_stale_sockets_of_any_version_lead_to_a_current_version_spawn(
    monkeypatch, state_dir, serving
):
    _stale_socket(state_dir / "laya-0.21.0.sock")
    _stale_socket(state_dir / "laya-0.22.0.sock")
    spawned: list[Path] = []

    class _Proc:
        def poll(self):
            return None

    def _spawn(path):
        spawned.append(path)
        threading.Thread(target=serving, args=(path, "current"), daemon=True).start()
        return _Proc()

    monkeypatch.setattr(server, "_spawn", _spawn)
    current = state_dir / "laya-0.23.0.sock"
    assert server.start(wait_s=10) == current
    assert spawned == [current]
    assert server.predict("y", {}) == {"current": "y"}
    assert spawned == [current], "a running current-version server must not be spawned again"


def test_a_server_in_a_group_or_world_accessible_dir_is_never_used(
    capsys, monkeypatch, state_dir, serving
):
    seen = []
    t = threading.Thread(
        target=server.serve,
        args=(state_dir / "laya-0.22.0.sock", lambda m, q: seen.append(m) or {"planted": m}),
        kwargs={"idle_s": 30},
        daemon=True,
    )
    t.start()
    deadline = time.monotonic() + 5
    while not (state_dir / "laya-0.22.0.sock").exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    os.chmod(state_dir, 0o755)
    try:
        monkeypatch.setattr(server, "_spawn", _no_spawn)
        assert server.predict("secret", {}) is None
        assert main(["classify-server", "start"]) == 1
        assert str(state_dir) in capsys.readouterr().err
        assert seen == []
    finally:
        os.chmod(state_dir, 0o700)
        server.request(state_dir / "laya-0.22.0.sock", None, None, timeout=5, op="shutdown")
        t.join(5)


def test_a_symlinked_socket_is_not_reused(monkeypatch, state_dir, serving):
    # Only a real socket file in our own dir counts; a symlink could point anywhere.
    elsewhere = Path(tempfile.mkdtemp(prefix="tcsr-", dir="/tmp"))
    try:
        os.chmod(elsewhere, 0o700)
        serving(elsewhere / "laya-0.22.0.sock", "elsewhere")
        (state_dir / "laya-0.22.0.sock").symlink_to(elsewhere / "laya-0.22.0.sock")
        spawned: list[Path] = []

        class _DeadProc:
            def poll(self):
                return 1

        def _spawn(path):
            spawned.append(path)
            return _DeadProc()

        monkeypatch.setattr(server, "_spawn", _spawn)
        with pytest.raises(server.StartError):
            server.start(wait_s=1)
        assert spawned == [state_dir / "laya-0.23.0.sock"]
    finally:
        shutil.rmtree(elsewhere, ignore_errors=True)
