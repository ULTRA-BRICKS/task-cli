"""tasklib.classify_server — the warm, machine-wide laya classify server (task-cli#245).

Drives the real socket server in a thread with an injected predictor (never the real model)
on a short throwaway socket path: AF_UNIX paths are capped near 104 bytes on macOS, which
pytest's own `tmp_path` can exceed.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import time
from pathlib import Path

import pytest

import tasklib.classify_server as server

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs AF_UNIX")


@pytest.fixture
def sock_path():
    d = tempfile.mkdtemp(prefix="tcs-", dir="/tmp")
    try:
        yield Path(d) / "s.sock"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _start(sock_path, predictor, *, idle_s=30.0):
    result: dict = {}

    def _run():
        result["rc"] = server.serve(sock_path, predictor, idle_s=idle_s)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    deadline = time.monotonic() + 5
    while not _accepting(sock_path):
        assert time.monotonic() < deadline, "server never started listening"
        time.sleep(0.01)
    return t, result


def _accepting(sock_path) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        try:
            probe.connect(str(sock_path))
        except OSError:
            return False
    return True


def _shutdown(t, sock_path):
    server.request(sock_path, None, None, timeout=5, op="shutdown")
    t.join(5)
    assert not t.is_alive()


def test_request_round_trips_message_and_questions_through_the_predictor(sock_path):
    seen = []

    def _predictor(message, questions):
        seen.append((message, questions))
        return {"answers": {"verdict": {"choice": "change"}}}

    t, _ = _start(sock_path, _predictor)
    questions = {"verdict": {"type": "choice", "criteria": {"change": "x"}}}
    assert server.request(sock_path, "fix it", questions, timeout=5) == {
        "answers": {"verdict": {"choice": "change"}}
    }
    assert server.request(sock_path, "and again", questions, timeout=5) is not None
    assert seen == [("fix it", questions), ("and again", questions)]
    assert stat.S_IMODE(os.stat(sock_path).st_mode) == 0o600
    _shutdown(t, sock_path)
    assert not sock_path.exists()


def test_a_predictor_failure_is_reported_and_the_server_keeps_serving(sock_path):
    def _predictor(message, questions):
        if message == "boom":
            raise ValueError("bad input")
        return {"ok": 1}

    t, _ = _start(sock_path, _predictor)
    assert server.request(sock_path, "boom", {}, timeout=5) is None
    assert server.request(sock_path, "fine", {}, timeout=5) == {"ok": 1}
    _shutdown(t, sock_path)


def test_an_unserializable_predictor_result_is_reported_and_the_server_keeps_serving(sock_path):
    def _predictor(message, questions):
        return {"score": {1, 2}} if message == "set" else {"ok": 1}

    t, result = _start(sock_path, _predictor)
    assert server.request(sock_path, "set", {}, timeout=5) is None
    assert server.request(sock_path, "fine", {}, timeout=5) == {"ok": 1}
    assert t.is_alive() and "rc" not in result
    _shutdown(t, sock_path)


def test_malformed_request_bytes_do_not_kill_the_server(sock_path):
    t, _ = _start(sock_path, lambda m, q: {"ok": 1})
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(sock_path))
        s.sendall(b"this is not json\n")
        reply = json.loads(s.makefile("rb").readline())
    assert reply["ok"] is False
    assert server.request(sock_path, "fine", {}, timeout=5) == {"ok": 1}
    _shutdown(t, sock_path)


def test_server_exits_and_removes_its_socket_after_the_idle_timeout(sock_path):
    t, result = _start(sock_path, lambda m, q: {}, idle_s=0.3)
    t.join(5)
    assert not t.is_alive()
    assert result["rc"] == 0
    assert not sock_path.exists()


def test_a_second_server_on_the_same_socket_backs_off(sock_path):
    t, _ = _start(sock_path, lambda m, q: {"first": True})
    assert server.serve(sock_path, lambda m, q: {"second": True}, idle_s=5) == 0
    assert server.request(sock_path, "x", {}, timeout=5) == {"first": True}
    _shutdown(t, sock_path)


def test_a_stale_socket_file_is_replaced(sock_path):
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(sock_path))
    stale.close()  # file stays behind, nobody listening
    t, _ = _start(sock_path, lambda m, q: {"fresh": True})
    assert server.request(sock_path, "x", {}, timeout=5) == {"fresh": True}
    _shutdown(t, sock_path)


def test_predict_returns_none_without_spawning_when_disabled(monkeypatch):
    def _no_spawn(*a, **kw):
        raise AssertionError("disabled server must never be spawned")

    monkeypatch.setattr(server, "_spawn", _no_spawn)
    assert server.predict("x", {}) is None


def test_predict_spawns_then_waits_for_the_socket(monkeypatch, sock_path):
    monkeypatch.delenv("TASK_CLASSIFY_SERVER")
    monkeypatch.setattr(server, "socket_path", lambda: sock_path)
    threads = []

    class _Proc:
        def poll(self):
            return None

    def _fake_spawn(path):
        t, _ = _start_later(path)
        threads.append(t)
        return _Proc()

    def _start_later(path):
        def _run():
            time.sleep(0.2)
            server.serve(path, lambda m, q: {"spawned": m}, idle_s=30)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return t, None

    monkeypatch.setattr(server, "_spawn", _fake_spawn)
    assert server.predict("hello server", {}) == {"spawned": "hello server"}
    assert len(threads) == 1
    # Warm now: a second call must not spawn again.
    assert server.predict("again", {}) == {"spawned": "again"}
    assert len(threads) == 1
    _shutdown(threads[0], sock_path)


def test_predict_gives_up_fast_when_the_spawned_server_dies(monkeypatch, sock_path):
    monkeypatch.delenv("TASK_CLASSIFY_SERVER")
    monkeypatch.setattr(server, "socket_path", lambda: sock_path)

    class _DeadProc:
        def poll(self):
            return 1

    monkeypatch.setattr(server, "_spawn", lambda path: _DeadProc())
    started = time.monotonic()
    assert server.predict("x", {}) is None
    assert time.monotonic() - started < 2


def test_predict_never_talks_to_a_socket_in_a_group_or_world_accessible_dir(
    monkeypatch, sock_path
):
    # Someone who can write the dir could plant a listener that answers as the model.
    monkeypatch.delenv("TASK_CLASSIFY_SERVER")
    monkeypatch.setattr(server, "socket_path", lambda: sock_path)
    t, _ = _start(sock_path, lambda m, q: {"planted": True})
    try:
        os.chmod(sock_path.parent, 0o755)
        monkeypatch.setattr(server, "_spawn", lambda path: pytest.fail("must not spawn"))
        assert server.predict("secret", {}) is None
        os.chmod(sock_path.parent, 0o700)
        assert server.predict("x", {}) == {"planted": True}
    finally:
        os.chmod(sock_path.parent, 0o700)
        _shutdown(t, sock_path)


class _LostRaceProc:
    """A spawned server that lost the singleton flock and exited cleanly."""

    def poll(self):
        return 0


def test_predict_gives_up_fast_when_it_lost_the_race_and_no_winner_holds_the_lock(
    monkeypatch, sock_path
):
    # The winner died mid-load (lock released, never bound): nothing will ever listen.
    monkeypatch.delenv("TASK_CLASSIFY_SERVER")
    monkeypatch.setattr(server, "socket_path", lambda: sock_path)
    monkeypatch.setattr(server, "_spawn", lambda path: _LostRaceProc())
    started = time.monotonic()
    assert server.predict("x", {}) is None
    assert time.monotonic() - started < 2


def test_predict_keeps_waiting_while_the_race_winner_still_holds_the_lock(monkeypatch, sock_path):
    import fcntl

    monkeypatch.delenv("TASK_CLASSIFY_SERVER")
    monkeypatch.setattr(server, "socket_path", lambda: sock_path)
    monkeypatch.setattr(server, "_spawn", lambda path: _LostRaceProc())
    monkeypatch.setattr(server, "_READY_TIMEOUT_S", 0.5)
    fd = os.open(str(sock_path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a winner still loading its model
        started = time.monotonic()
        assert server.predict("x", {}) is None
        assert time.monotonic() - started >= 0.5
    finally:
        os.close(fd)
