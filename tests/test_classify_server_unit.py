"""The laya server runs under its own service-manager unit when one is installed (task-cli#282).

`start`/`predict` first reuse a running server; otherwise they start the installed
`ai.hyperide.laya` unit (`systemctl --user start`, or `launchctl kickstart` on macOS) and only
spawn a detached server when no unit exists. The unit's ExecStart is `task classify-server
serve`. Fake `systemctl`/`launchctl` scripts on PATH log their arguments and, on start, launch a
fake server (canned predictor, no model) on the socket. Sockets live on a short /tmp path
(AF_UNIX paths are capped near 104 bytes on macOS).
"""

from __future__ import annotations

import os
import shutil
import socket
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
# conftest replaces `_start_unit` for every test; these tests drive the real one.
REAL_START_UNIT = server._start_unit
UNIT = "ai.hyperide.laya.service"

FAKE_SERVER = """
import sys
from pathlib import Path
import tasklib.classify_server as server
raise SystemExit(server.serve(Path(sys.argv[1]), lambda m, q: {"unit": m}, idle_s=30))
"""

# One script for both managers; FAKE_MANAGER_* env vars decide what it does.
FAKE_MANAGER = """#!{python}
import os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_MANAGER_LOG"], "a") as log:
    log.write(os.path.basename(sys.argv[0]) + " " + " ".join(args) + "\\n")
installed = os.environ.get("FAKE_UNIT_INSTALLED") == "1"
if args[:2] == ["--user", "cat"]:
    sys.exit(0 if installed else 1)
if args[:2] == ["--user", "show-environment"]:
    print(os.environ.get("FAKE_MANAGER_ENV", "PATH=/usr/bin"))
    sys.exit(0)
# FAKE_UNIT_DIES: the server fails; FAKE_UNIT_EXITS: it exits 0 without listening. Neither
# launches the fake server.
dies = os.environ.get("FAKE_UNIT_DIES") == "1"
exits = os.environ.get("FAKE_UNIT_EXITS") == "1"
if args[:2] == ["--user", "show"]:
    print("failed" if dies else "inactive" if exits else "active")
    sys.exit(0)
if args[:1] == ["print"]:
    if dies or exits:
        print("\\tstate = not running\\n\\tlast exit code = " + ("1" if dies else "0"))
    else:
        print("\\tstate = running\\n\\tlast exit code = 1")  # a stale code from an earlier run
    sys.exit(0)
if args[:2] == ["--user", "start"] or args[:1] == ["kickstart"]:
    if os.environ.get("FAKE_UNIT_START_FAILS") == "1":
        print("Failed to start unit: Unit not found.", file=sys.stderr)
        sys.exit(5)
    if not (dies or exits):
        # Like the real managers, return at once: the server must not hold our output pipes.
        subprocess.Popen(
            [sys.executable, "-c", os.environ["FAKE_SERVER"], os.environ["FAKE_SOCK"]],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    sys.exit(0)
sys.exit(2)
"""


@pytest.fixture
def unit_env(monkeypatch):
    """A private state dir, the real `_start_unit`, and fake managers first on PATH.

    The state dir is where a unit's server would bind: the default one under the (faked)
    account home, which the fake service manager's environment does not override.
    """
    monkeypatch.delenv("TASK_CLASSIFY_SERVER")
    monkeypatch.delenv("TASK_CLASSIFY_UNIT", raising=False)
    monkeypatch.setattr(server, "_start_unit", REAL_START_UNIT)
    monkeypatch.setattr(server, "_UNIT_POLL_INTERVAL_S", 0.05)
    d = Path(tempfile.mkdtemp(prefix="tcsu-", dir="/tmp"))
    os.chmod(d, 0o700)
    home, bin_dir = d / "home", d / "bin"
    state = home / ".local" / "state" / "task-cli" / "classify"
    state.mkdir(parents=True, mode=0o700)
    bin_dir.mkdir()
    monkeypatch.setattr(server, "_account_home", lambda: str(home))
    sock = state / "laya-0.24.0.sock"
    monkeypatch.setattr(server, "socket_path", lambda: sock)
    for name in ("systemctl", "launchctl"):
        script = bin_dir / name
        script.write_text(FAKE_MANAGER.format(python=sys.executable))
        script.chmod(0o755)
    log = d / "manager.log"
    log.touch()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_MANAGER_LOG", str(log))
    monkeypatch.setenv("FAKE_SERVER", FAKE_SERVER)
    monkeypatch.setenv("FAKE_SOCK", str(sock))
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("FAKE_UNIT_INSTALLED", "1")
    env = {"dir": d, "sock": sock, "log": log}
    try:
        yield env
    finally:
        try:
            server.request(sock, None, None, timeout=5, op="shutdown")
        except OSError:
            pass
        deadline = time.monotonic() + 5
        while sock.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        shutil.rmtree(d, ignore_errors=True)


def _calls(env) -> list[str]:
    return env["log"].read_text().splitlines()


def _no_spawn(path):
    raise AssertionError("must not spawn: the unit runs the server")


def test_start_starts_the_installed_unit_and_never_spawns(capsys, monkeypatch, unit_env):
    monkeypatch.setattr(server, "_spawn", _no_spawn)
    rc = main(["classify-server", "start"])
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert out == f"{unit_env['sock']}\n"
    assert f"systemctl --user cat {UNIT}" in _calls(unit_env)
    assert f"systemctl --user start {UNIT}" in _calls(unit_env)
    assert server.request(unit_env["sock"], "hi", {}, timeout=5) == {"unit": "hi"}


def test_predict_starts_the_installed_unit_and_never_spawns(monkeypatch, unit_env):
    monkeypatch.setattr(server, "_spawn", _no_spawn)
    assert server.predict("hello", {}) == {"unit": "hello"}
    assert f"systemctl --user start {UNIT}" in _calls(unit_env)


def test_a_running_server_is_reused_without_asking_the_service_manager(
    capsys, monkeypatch, unit_env
):
    old = unit_env["sock"].with_name("laya-0.23.1.sock")
    t = threading.Thread(
        target=server.serve, args=(old, lambda m, q: {"old": m}), kwargs={"idle_s": 30},
        daemon=True,
    )
    t.start()
    deadline = time.monotonic() + 5
    while not old.exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    monkeypatch.setattr(server, "_spawn", _no_spawn)
    try:
        assert main(["classify-server", "start"]) == 0
        assert capsys.readouterr().out == f"{old}\n"
        assert _calls(unit_env) == []
    finally:
        server.request(old, None, None, timeout=5, op="shutdown")
        t.join(5)


def test_without_a_unit_start_spawns_as_before(capsys, monkeypatch, unit_env):
    monkeypatch.setenv("FAKE_UNIT_INSTALLED", "0")
    spawned: list[Path] = []

    def _spawn(path):
        spawned.append(path)
        return subprocess.Popen(
            [sys.executable, "-c", FAKE_SERVER, str(path)], start_new_session=True
        )

    monkeypatch.setattr(server, "_spawn", _spawn)
    rc = main(["classify-server", "start"])
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert out == f"{unit_env['sock']}\n"
    assert spawned == [unit_env["sock"]]
    assert _calls(unit_env) == [f"systemctl --user cat {UNIT}"]


def test_without_systemctl_on_path_start_spawns_as_before(monkeypatch, unit_env):
    empty = unit_env["dir"] / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    spawned: list[Path] = []

    def _spawn(path):
        spawned.append(path)
        return subprocess.Popen(
            [sys.executable, "-c", FAKE_SERVER, str(path)], start_new_session=True
        )

    monkeypatch.setattr(server, "_spawn", _spawn)
    assert server.start(wait_s=10) == unit_env["sock"]
    assert spawned == [unit_env["sock"]]


def test_a_unit_that_will_not_start_fails_naming_the_unit(capsys, monkeypatch, unit_env):
    monkeypatch.setenv("FAKE_UNIT_START_FAILS", "1")
    monkeypatch.setattr(server, "_spawn", _no_spawn)
    rc = main(["classify-server", "start"])
    out, err = capsys.readouterr()
    assert rc == 1
    assert out == ""
    assert len(err.strip().splitlines()) == 1
    assert f"systemctl --user start {UNIT}" in err
    assert "Unit not found" in err


def test_a_unit_that_exits_without_listening_is_reported_fast(capsys, monkeypatch, unit_env):
    monkeypatch.setenv("FAKE_UNIT_EXITS", "1")
    monkeypatch.setattr(server, "_spawn", _no_spawn)
    started = time.monotonic()
    rc = main(["classify-server", "start"])
    _, err = capsys.readouterr()
    assert rc == 1
    assert f"{UNIT} exited before listening" in err
    assert time.monotonic() - started < 5, "a stopped unit must not wait out the 60 s cap"


def test_a_unit_that_lost_the_race_waits_for_the_server_holding_the_lock(monkeypatch, unit_env):
    # The unit's server exited 0 because another server (here: a slow-loading one on the same
    # socket) holds the singleton lock; start must wait for that one, not report a failure.
    monkeypatch.setenv("FAKE_UNIT_EXITS", "1")
    monkeypatch.setattr(server, "_spawn", _no_spawn)
    sock = unit_env["sock"]
    winner = threading.Thread(
        target=server.serve,
        args=(sock, None),
        kwargs={"idle_s": 30, "load": lambda: (time.sleep(1.0), lambda m, q: {"w": m})[1]},
        daemon=True,
    )
    winner.start()
    deadline = time.monotonic() + 5
    while not server._lock_held(sock):
        assert time.monotonic() < deadline, "winner never took the lock"
        time.sleep(0.01)
    assert server.start(wait_s=10) == sock
    assert server.request(sock, "x", {}, timeout=5) == {"w": "x"}


def test_a_unit_that_starts_then_fails_is_reported_fast(capsys, monkeypatch, unit_env):
    monkeypatch.setenv("FAKE_UNIT_DIES", "1")
    monkeypatch.setattr(server, "_spawn", _no_spawn)
    started = time.monotonic()
    rc = main(["classify-server", "start"])
    _, err = capsys.readouterr()
    assert rc == 1
    assert f"{UNIT} failed" in err and f"journalctl --user -u {UNIT}" in err
    assert time.monotonic() - started < 5, "a failed unit must not wait out the 60 s cap"


def test_the_unit_name_can_be_overridden(capsys, monkeypatch, unit_env):
    monkeypatch.setenv("TASK_CLASSIFY_UNIT", "my-laya.service")
    monkeypatch.setattr(server, "_spawn", _no_spawn)
    assert main(["classify-server", "start"]) == 0
    assert "systemctl --user start my-laya.service" in _calls(unit_env)


def _install_launch_agent(unit_env) -> None:
    agents = unit_env["dir"] / "home" / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "ai.hyperide.laya.plist").write_text("<plist/>")


def test_macos_kickstarts_the_launch_agent(capsys, monkeypatch, unit_env):
    _install_launch_agent(unit_env)
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server, "_spawn", _no_spawn)
    assert server.start(wait_s=10) == unit_env["sock"]
    assert _calls(unit_env)[0] == f"launchctl kickstart gui/{os.getuid()}/ai.hyperide.laya"


def test_macos_a_launch_agent_that_fails_is_reported_fast(monkeypatch, unit_env):
    _install_launch_agent(unit_env)
    monkeypatch.setenv("FAKE_UNIT_DIES", "1")
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server, "_spawn", _no_spawn)
    started = time.monotonic()
    target = f"gui/{os.getuid()}/ai.hyperide.laya"
    expected = f"ai.hyperide.laya failed .*launchctl print {target}"
    with pytest.raises(server.StartError, match=expected):
        server.start()
    assert time.monotonic() - started < 5, "a failed agent must not wait out the 60 s cap"


def test_macos_without_the_launch_agent_spawns(monkeypatch, unit_env):
    monkeypatch.setattr(server.sys, "platform", "darwin")

    class _DeadProc:
        def poll(self):
            return 1

    spawned: list[Path] = []
    monkeypatch.setattr(server, "_spawn", lambda path: spawned.append(path) or _DeadProc())
    with pytest.raises(server.StartError, match="exited with status 1"):
        server.start(wait_s=5)
    assert spawned == [unit_env["sock"]]
    assert _calls(unit_env) == []


def test_a_unit_whose_server_would_bind_another_state_dir_is_not_used(monkeypatch, unit_env):
    # The unit runs with the manager's environment: here its XDG_STATE_HOME points elsewhere,
    # so its server could never answer on our socket; spawn instead of waiting 60 s for it.
    monkeypatch.setenv("FAKE_MANAGER_ENV", f"XDG_STATE_HOME={unit_env['dir'] / 'elsewhere'}")
    spawned: list[Path] = []

    def _spawn(path):
        spawned.append(path)
        return subprocess.Popen(
            [sys.executable, "-c", FAKE_SERVER, str(path)], start_new_session=True
        )

    monkeypatch.setattr(server, "_spawn", _spawn)
    assert server.start(wait_s=10) == unit_env["sock"]
    assert spawned == [unit_env["sock"]]
    assert f"systemctl --user start {UNIT}" not in _calls(unit_env)
    assert "systemctl --user show-environment" in _calls(unit_env)


def test_serve_binds_the_socket_and_exits_0_when_idle(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # serve moves into the socket dir; restore the cwd afterwards
    d = Path(tempfile.mkdtemp(prefix="tcsv-", dir="/tmp"))
    sock = d / "classify" / "laya-0.24.0.sock"
    monkeypatch.setattr(server, "socket_path", lambda: sock)
    monkeypatch.setattr(server, "IDLE_SECONDS", 0.5)
    monkeypatch.setattr(server, "_load_laya_predictor", lambda: (lambda m, q: {"served": m}))
    result: dict = {}
    t = threading.Thread(target=lambda: result.update(rc=main(["classify-server", "serve"])))
    try:
        t.start()
        deadline = time.monotonic() + 5
        while not sock.exists():
            assert time.monotonic() < deadline, "serve never bound its socket"
            time.sleep(0.01)
        assert server.request(sock, "x", {}, timeout=5) == {"served": "x"}
        assert (sock.parent.stat().st_mode & 0o777) == 0o700
        t.join(10)
        assert not t.is_alive(), "serve must exit after the idle timeout"
        assert result["rc"] == 0
        assert not sock.exists()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_a_prerelease_socket_does_not_outrank_its_final_release():
    names = ["laya-0.23.1rc1.sock", "laya-0.23.1.sock", "laya-0.9.0.sock", "laya-x.sock"]
    ordered = sorted((Path(n) for n in names), key=server._version_key, reverse=True)
    assert [p.name for p in ordered] == [
        "laya-0.23.1.sock",
        "laya-0.23.1rc1.sock",
        "laya-0.9.0.sock",
        "laya-x.sock",
    ]
