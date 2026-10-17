"""`task classify-server start` — start the warm laya server on demand (task-cli#279).

text-lint calls this when the local Laya socket does not answer. The server here is a fake
module run as a real detached process: it binds the socket through `serve` with a canned
predictor, so no model is ever loaded. Sockets live on a short /tmp path (AF_UNIX paths are
capped near 104 bytes on macOS, which pytest's `tmp_path` can exceed).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

import tasklib.classify_server as server
from tasklib.cli import main

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs AF_UNIX")

REPO_ROOT = Path(__file__).resolve().parents[1]

FAKE_SERVER = """
import sys
from pathlib import Path
import tasklib.classify_server as server
raise SystemExit(server.serve(Path(sys.argv[1]), lambda m, q: {"fake": m}, idle_s=30))
"""


@pytest.fixture
def sock_path(monkeypatch):
    monkeypatch.delenv("TASK_CLASSIFY_SERVER")
    d = tempfile.mkdtemp(prefix="tcss-", dir="/tmp")
    os.chmod(d, 0o700)
    path = Path(d) / "s.sock"
    monkeypatch.setattr(server, "socket_path", lambda: path)
    try:
        yield path
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def fake_spawns(monkeypatch, sock_path):
    """Replace `_spawn` with one that runs FAKE_SERVER detached; yields the spawned procs."""
    procs: list[subprocess.Popen] = []

    def _spawn(path):
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
        proc = subprocess.Popen(
            [sys.executable, "-c", FAKE_SERVER, str(path)],
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        procs.append(proc)
        return proc

    monkeypatch.setattr(server, "_spawn", _spawn)
    try:
        yield procs
    finally:
        for proc in procs:
            if proc.poll() is None:
                try:
                    server.request(sock_path, None, None, timeout=5, op="shutdown")
                except OSError:
                    pass
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(5)


def test_start_when_absent(capsys, sock_path, fake_spawns):
    assert not sock_path.exists()
    rc = main(["classify-server", "start"])
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert out == f"{sock_path}\n"
    assert err == ""
    assert len(fake_spawns) == 1
    # The server it started answers.
    assert server.request(sock_path, "hi", {}, timeout=5) == {"fake": "hi"}


def test_start_when_running(capsys, sock_path, fake_spawns):
    assert main(["classify-server", "start"]) == 0
    capsys.readouterr()
    rc = main(["classify-server", "start"])
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert out == f"{sock_path}\n"
    assert len(fake_spawns) == 1, "a running server must not be spawned again"


def test_start_fails(capsys, monkeypatch, sock_path):
    class _DeadProc:
        def poll(self):
            return 3

    monkeypatch.setattr(server, "_spawn", lambda path: _DeadProc())
    started = time.monotonic()
    rc = main(["classify-server", "start"])
    out, err = capsys.readouterr()
    assert rc == 1
    assert out == ""
    assert len(err.strip().splitlines()) == 1
    assert "exited with status 3" in err
    assert str(sock_path.with_suffix(".log")) in err
    assert time.monotonic() - started < 2, "a dead server must fail fast, not wait out the cap"


def test_start_fails_when_spawn_raises(capsys, monkeypatch, sock_path):
    def _boom(path):
        raise OSError("no such interpreter")

    monkeypatch.setattr(server, "_spawn", _boom)
    rc = main(["classify-server", "start"])
    out, err = capsys.readouterr()
    assert rc == 1
    assert out == ""
    assert "no such interpreter" in err
    assert len(err.strip().splitlines()) == 1


def test_start_fails_when_python_cannot_spawn_safely(capsys, monkeypatch, sock_path):
    monkeypatch.setattr(server, "_spawn", lambda path: None)
    rc = main(["classify-server", "start"])
    _, err = capsys.readouterr()
    assert rc == 1
    assert "-P" in err


def test_start_refuses_when_disabled(capsys, monkeypatch):
    monkeypatch.setenv("TASK_CLASSIFY_SERVER", "0")
    monkeypatch.setattr(server, "_spawn", lambda path: pytest.fail("must not spawn"))
    rc = main(["classify-server", "start"])
    out, err = capsys.readouterr()
    assert rc == 1
    assert out == ""
    assert "disabled" in err and "TASK_CLASSIFY_SERVER" in err
    assert len(err.strip().splitlines()) == 1


def test_start_refuses_a_socket_dir_others_can_access(capsys, monkeypatch, sock_path):
    os.chmod(sock_path.parent, 0o755)
    monkeypatch.setattr(server, "_spawn", lambda path: pytest.fail("must not spawn"))
    rc = main(["classify-server", "start"])
    _, err = capsys.readouterr()
    assert rc == 1
    assert str(sock_path.parent) in err


def test_start_times_out_when_the_server_never_listens(monkeypatch, sock_path):
    class _HungProc:
        def poll(self):
            return None

    monkeypatch.setattr(server, "_spawn", lambda path: _HungProc())
    started = time.monotonic()
    with pytest.raises(server.StartError, match="within 0.3 s"):
        server.start(wait_s=0.3)
    assert time.monotonic() - started >= 0.3


def test_start_waits_as_long_as_predict_by_default():
    # A cold load measured ~25 s under heavy load; start must not give up sooner than predict.
    import inspect

    default = inspect.signature(server.start).parameters["wait_s"].default
    assert default == server._READY_TIMEOUT_S == 60.0


def test_concurrent_start_waits_for_the_race_winner(monkeypatch, sock_path):
    # Our spawn lost the singleton flock (exit 0) while the winner still loads its model:
    # start keeps waiting and returns once the winner binds.
    import threading

    class _LostRaceProc:
        def poll(self):
            return 0

    winner = threading.Thread(
        target=server.serve,
        args=(sock_path, None),
        kwargs={"idle_s": 30, "load": lambda: (time.sleep(0.5), lambda m, q: {"w": m})[1]},
        daemon=True,
    )
    winner.start()
    deadline = time.monotonic() + 5
    while not server._lock_held(sock_path):
        assert time.monotonic() < deadline, "winner never took the lock"
        time.sleep(0.01)
    monkeypatch.setattr(server, "_spawn", lambda path: _LostRaceProc())
    try:
        assert server.start(wait_s=10) == sock_path
        assert server.request(sock_path, "x", {}, timeout=5) == {"w": "x"}
    finally:
        server.request(sock_path, None, None, timeout=5, op="shutdown")
        winner.join(5)
