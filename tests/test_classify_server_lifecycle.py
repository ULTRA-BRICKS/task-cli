"""Warm laya server lifecycle: configurable idle lifetime, signals, service entrypoint (task-cli#298).

The model is never loaded: `serve` runs with a canned predictor, in a thread for the
in-process cases and in a real subprocess (the same foreground path the module entrypoint
takes) for signal handling. Sockets live on a short /tmp path (AF_UNIX paths are capped near
104 bytes on macOS, which pytest's `tmp_path` can exceed).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

import tasklib.classify_server as server
from tasklib.cli import main

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs AF_UNIX")

REPO_ROOT = Path(__file__).resolve().parents[1]
IDLE = server.IDLE_ENV

# What `python -m tasklib.classify_server` does, minus the real model: the predictor sleeps for
# the number of seconds the request message names, so a test can signal a request in flight.
FOREGROUND = """
import sys, time
from pathlib import Path
import tasklib.classify_server as server

def load():
    def predict(message, questions):
        time.sleep(float(message))
        return {"slept": message}
    return predict

raise SystemExit(server._run_foreground(Path(sys.argv[1]), load))
"""

FAKE_SERVER = """
import sys
from pathlib import Path
import tasklib.classify_server as server
raise SystemExit(server.serve(Path(sys.argv[1]), lambda m, q: {"fake": m}, idle_s=30))
"""


@pytest.fixture
def sock_path(monkeypatch):
    monkeypatch.delenv("TASK_CLASSIFY_SERVER")
    monkeypatch.delenv(IDLE, raising=False)
    d = tempfile.mkdtemp(prefix="tcsl-", dir="/tmp")
    os.chmod(d, 0o700)
    path = Path(d) / "s.sock"
    monkeypatch.setattr(server, "socket_path", lambda: path)
    try:
        yield path
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _accepting(path: Path) -> bool:
    try:
        server._probe(path)
    except OSError:
        return False
    return True


def _wait_accepting(path: Path, proc=None) -> None:
    deadline = time.monotonic() + 10
    while not _accepting(path):
        assert proc is None or proc.poll() is None, "the server exited before listening"
        assert time.monotonic() < deadline, "server never started listening"
        time.sleep(0.02)


def _serve_in_thread(path: Path, predictor, *, idle_s):
    result: dict = {}

    def _run():
        result["rc"] = server.serve(path, predictor, idle_s=idle_s)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    _wait_accepting(path)
    return t, result


def _foreground(path: Path, *, idle: str | None):
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    env.pop(IDLE, None)
    if idle is not None:
        env[IDLE] = idle
    return subprocess.Popen(
        [sys.executable, "-P", "-c", FOREGROUND, str(path)],
        stdin=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )


# --- the setting -------------------------------------------------------------------------


def test_default_idle_is_still_fifteen_minutes():
    assert server.IDLE_SECONDS == 900.0
    assert server.idle_seconds({}) == 900.0


@pytest.mark.parametrize(
    ("raw", "expected"), [("0", 0.0), ("2.5", 2.5), (" 60 ", 60.0), ("1e3", 1000.0)]
)
def test_idle_setting_accepts_finite_nonnegative_numbers(raw, expected):
    assert server.idle_seconds({IDLE: raw}) == expected


@pytest.mark.parametrize("raw", ["", "   ", "-1", "-0.5", "nan", "inf", "-inf", "abc", "1,5"])
def test_idle_setting_rejects_everything_else_by_name(raw):
    with pytest.raises(server.IdleSettingError) as exc:
        server.idle_seconds({IDLE: raw})
    assert IDLE in str(exc.value)
    assert repr(raw) in str(exc.value)


def test_a_huge_finite_idle_is_accepted_and_never_overflows_the_wait(sock_path):
    assert server.idle_seconds({IDLE: "1e308"}) == 1e308
    t, result = _serve_in_thread(sock_path, lambda m, q: {"echo": m}, idle_s=1e308)
    assert server.request(sock_path, "x", {}, timeout=5) == {"echo": "x"}
    server.request(sock_path, None, None, timeout=5, op="shutdown")
    t.join(5)
    assert result["rc"] == 0


def test_service_entry_creates_the_private_state_dir_and_runs_from_it(monkeypatch, sock_path):
    # A supervisor starts the module in an arbitrary cwd; the model must still load from the
    # server's own state dir (the cwd-shadowing guard in LayaClassifier._load_agent).
    monkeypatch.chdir(sock_path.parent)  # registers the undo for the chdir below
    monkeypatch.setenv("XDG_STATE_HOME", str(sock_path.parent / "xdg"))
    default = server._state_dir() / "s.sock"
    server._enter_state_dir(default)
    assert Path.cwd().resolve() == default.parent.resolve()
    assert stat.S_IMODE(default.parent.stat().st_mode) == 0o700


def test_service_entry_never_chmods_a_custom_socket_directory(monkeypatch, sock_path):
    # `python -m tasklib.classify_server /tmp/x.sock` must not take over /tmp's permissions.
    monkeypatch.chdir(sock_path.parent)
    shared = sock_path.parent / "shared"
    shared.mkdir()
    os.chmod(shared, 0o755)
    monkeypatch.setattr(server, "_run_foreground", lambda path, load: 0)
    assert server.main([str(shared / "s.sock")]) == 0
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755
    assert Path.cwd().resolve() == shared.resolve()


# --- serve ------------------------------------------------------------------------------


def test_zero_idle_stays_up_without_spinning_and_serves_a_later_request(sock_path):
    t, result = _serve_in_thread(sock_path, lambda m, q: {"echo": m}, idle_s=0)
    cpu = time.process_time()
    assert server.request(sock_path, "first", {}, timeout=5) == {"echo": "first"}
    time.sleep(0.6)
    assert t.is_alive() and "rc" not in result, "idle_s=0 must never exit on idle"
    # A zero socket timeout would make accept() fail instantly in a loop and burn a core.
    assert time.process_time() - cpu < 0.3
    assert server.request(sock_path, "second", {}, timeout=5) == {"echo": "second"}
    server.request(sock_path, None, None, timeout=5, op="shutdown")
    t.join(5)
    assert result["rc"] == 0 and not sock_path.exists()


def test_a_finite_idle_still_exits_and_removes_the_socket(sock_path):
    t, result = _serve_in_thread(sock_path, lambda m, q: {}, idle_s=0.3)
    t.join(5)
    assert not t.is_alive() and result["rc"] == 0
    assert not sock_path.exists()


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_serve_refuses_an_invalid_idle_before_touching_anything(sock_path, bad):
    with pytest.raises(ValueError):
        server.serve(sock_path, lambda m, q: {}, idle_s=bad)
    assert not sock_path.exists()
    assert not Path(str(sock_path) + ".lock").exists()


# --- the foreground entrypoint ----------------------------------------------------------


def test_foreground_passes_the_default_when_the_setting_is_unset(monkeypatch, sock_path):
    seen = {}
    monkeypatch.setattr(server, "serve", lambda path, predictor, **kw: seen.update(kw) or 0)
    assert server._run_foreground(sock_path, lambda: None, env={}) == 0
    assert seen["idle_s"] == 900.0


def test_foreground_passes_the_parsed_setting_to_serve(monkeypatch, sock_path):
    seen = {}
    monkeypatch.setattr(server, "serve", lambda path, predictor, **kw: seen.update(kw) or 0)
    assert server._run_foreground(sock_path, lambda: None, env={IDLE: "0"}) == 0
    assert seen["idle_s"] == 0.0


def test_foreground_restores_the_signal_handlers_it_installed(monkeypatch, sock_path):
    monkeypatch.setattr(server, "serve", lambda path, predictor, **kw: 0)
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    server._run_foreground(sock_path, lambda: None, env={})
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == before


def test_foreground_process_honours_the_setting_end_to_end(sock_path):
    proc = _foreground(sock_path, idle="0.4")
    try:
        _wait_accepting(sock_path, proc)
        assert proc.wait(10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stderr.close()
    assert not sock_path.exists()


def test_foreground_process_rejects_an_invalid_setting_before_taking_the_lock(sock_path):
    proc = _foreground(sock_path, idle="-5")
    err = proc.communicate(timeout=10)[1].decode()
    assert proc.returncode == 2
    assert IDLE in err and "-5" in err
    assert not Path(str(sock_path) + ".lock").exists()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_removes_the_socket_releases_the_lock_and_exits_zero(sock_path, sig):
    proc = _foreground(sock_path, idle="0")
    try:
        _wait_accepting(sock_path, proc)
        assert server.request(sock_path, "0", {}, timeout=5) == {"slept": "0"}
        proc.send_signal(sig)
        assert proc.wait(10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(5)
        proc.stderr.close()
    assert not sock_path.exists()
    assert not server._lock_held(sock_path)


def test_a_signal_lets_the_in_flight_request_finish(sock_path):
    proc = _foreground(sock_path, idle="0")
    reply: dict = {}
    try:
        _wait_accepting(sock_path, proc)
        worker = threading.Thread(
            target=lambda: reply.update(r=server.request(sock_path, "1.0", {}, timeout=10))
        )
        worker.start()
        time.sleep(0.4)  # the request is inside the predictor now
        proc.send_signal(signal.SIGTERM)
        worker.join(10)
        assert reply["r"] == {"slept": "1.0"}
        assert proc.wait(10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(5)
        proc.stderr.close()
    assert not sock_path.exists()


# --- client side: start / spawn / predict -----------------------------------------------


def test_start_refuses_an_invalid_setting_without_spawning(capsys, monkeypatch, sock_path):
    monkeypatch.setenv(IDLE, "-1")
    spawned = []
    monkeypatch.setattr(server, "_spawn", lambda path: spawned.append(path))
    assert main(["classify-server", "start"]) == 1
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1
    assert err.startswith("error:") and IDLE in err
    assert spawned == []


def test_start_refuses_an_invalid_setting_even_when_a_server_is_already_up(
    capsys, monkeypatch, sock_path
):
    t, _ = _serve_in_thread(sock_path, lambda m, q: {}, idle_s=30)
    try:
        monkeypatch.setenv(IDLE, "nan")
        assert main(["classify-server", "start"]) == 1
        assert IDLE in capsys.readouterr().err
    finally:
        server.request(sock_path, None, None, timeout=5, op="shutdown")
        t.join(5)


def test_the_spawned_server_inherits_the_setting(monkeypatch, sock_path):
    captured = {}

    class _Popen:
        def __init__(self, argv, **kw):
            captured["env"] = kw["env"]

    monkeypatch.setattr(server.subprocess, "Popen", _Popen)
    monkeypatch.setenv(IDLE, "0")
    server._spawn(sock_path)
    assert captured["env"][IDLE] == "0"


def test_an_unset_setting_is_not_invented_for_the_spawned_server(monkeypatch, sock_path):
    captured = {}

    class _Popen:
        def __init__(self, argv, **kw):
            captured["env"] = kw["env"]

    monkeypatch.setattr(server.subprocess, "Popen", _Popen)
    server._spawn(sock_path)
    assert IDLE not in captured["env"]


def test_predict_reports_an_invalid_setting_and_falls_back(capsys, monkeypatch, sock_path):
    monkeypatch.setenv(IDLE, "-1")
    monkeypatch.setattr(server, "_spawn", lambda path: pytest.fail("must not spawn"))
    assert server.predict("hi", {}) is None
    assert IDLE in capsys.readouterr().err


def _exited_within(proc: subprocess.Popen, seconds: float) -> bool:
    try:
        proc.wait(seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


def test_concurrent_starts_leave_exactly_one_server(monkeypatch, sock_path):
    procs: list[subprocess.Popen] = []
    lock = threading.Lock()

    def _spawn(path):
        proc = subprocess.Popen(
            [sys.executable, "-c", FAKE_SERVER, str(path)],
            stdin=subprocess.DEVNULL,
            env=dict(os.environ, PYTHONPATH=str(REPO_ROOT)),
            start_new_session=True,
        )
        with lock:
            procs.append(proc)
        return proc

    monkeypatch.setattr(server, "_spawn", _spawn)
    results: list = []
    errors: list = []

    def _start():
        try:
            results.append(server.start(wait_s=30))
        except server.StartError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_start) for _ in range(4)]
    try:
        for th in threads:
            th.start()
        for th in threads:
            th.join(40)
        assert errors == []
        assert results == [sock_path] * 4
        for proc in procs:  # racers that lost the flock exit 0; the winner keeps serving
            _exited_within(proc, 2)
        alive = [p for p in procs if p.poll() is None]
        assert len(alive) == 1
        assert all(p.returncode == 0 for p in procs if p.poll() is not None)
        assert server.request(sock_path, "x", {}, timeout=5) == {"fake": "x"}
    finally:
        with contextlib.suppress(OSError):
            server.request(sock_path, None, None, timeout=5, op="shutdown")
        for proc in procs:
            if not _exited_within(proc, 5):
                proc.kill()
                proc.wait(5)
