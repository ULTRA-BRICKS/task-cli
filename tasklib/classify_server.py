"""A warm, machine-wide laya classify server so `task classify` stops reloading the model.

WHY (task-cli#245): loading the laya checkpoint costs ~3 s of every `task classify` call
(measured: torch import 0.5 s, `laya.load` 2.8 s, `predict` 0.1 s), and each call is a fresh
process, so the tg-cli inbound hook and grooming passes paid 4-11 s per message. This module
keeps ONE loaded model resident in a detached process and answers `predict` requests over a
unix socket, so a warm call is one socket round-trip.

HOW it is reached: `LayaClassifier.classify` calls :func:`predict`. When no server is listening,
:func:`predict` starts one (see "Who runs the server" below) and waits until the socket accepts;
a later call just connects. Every failure (start refused, server died, socket error, bad reply)
returns ``None`` and the caller loads the model in-process exactly as before — the server is an
accelerator, never a new way for classify to fail.

``task classify-server start`` (:func:`start`, task-cli#279) is the same start-and-wait for a
caller that NEEDS the server (text-lint, where laya is mandatory): it returns the socket path once
the socket accepts, or raises :class:`StartError` with the reason instead of falling back.

Who runs the server: when the service-manager unit ``ai.hyperide.laya.service`` (override with
``TASK_CLASSIFY_UNIT``) is installed, the server runs under it, not inside whichever process
asked for it, and is started with ``systemctl --user start <unit>`` (Linux: the unit is
installed when ``systemctl --user cat <unit>`` succeeds) or ``launchctl kickstart
gui/<uid>/<label>`` (macOS: ``~/Library/LaunchAgents/<label>.plist`` exists; ``<label>`` is the
unit name without ``.service``; an agent that is not loaded yet is loaded with ``launchctl
bootstrap gui/<uid> <plist>`` and kickstarted once more). The unit's ExecStart runs ``task
classify-server serve`` (:func:`run_server`, foreground). The client spawns ``python -P -m
tasklib.classify_server`` detached itself (the same interpreter/env bootstrap `task daemon
start` uses — `daemon._child_env` etc.) only when no unit is installed, or when the unit's
server would bind another state dir than the client's (see :func:`_start_unit`).

INVARIANTS:
- One server per user at a time: the socket lives under the user's state dir
  (``$XDG_STATE_HOME/task-cli/classify/``, mode 0700; the socket itself 0600) and its name
  carries ``tasklib.__version__``. Before spawning, a client reuses ANY running
  ``laya-*.sock`` in that dir, highest version first (task-cli#282): right after an upgrade
  the previous version's server is still up, and a second one would load a second ~2 GB model.
  The current-version server is started only when none answers. This relies on every version
  answering the same requests the same way: a change to the request format, to the model id, or
  to the server-side load/predict code must also change the ``laya-`` socket prefix, so an older
  server is never reused for a client that expects different answers.
- The request carries the QUESTIONS as well as the message, so the server holds no copy of the
  classification schema that could drift from the client's.
- A flock on ``<socket>.lock`` is taken BEFORE the model loads: two cold clients racing to spawn
  load the model once; the loser exits 0 and the client keeps waiting for the winner's socket.
- The server exits after :data:`IDLE_SECONDS` without a request (the model holds ~1 GB of RAM),
  removing its socket. A server killed without cleanup leaves a stale socket file; the next
  client gets "connection refused", spawns, and the new server (holding the lock) replaces it.
- The model id is the same fixed `_LAYA_DEFAULT_MODEL_ID` the in-process path uses, loaded with
  the server's cwd set to its own state dir (see `LayaClassifier._load_agent`'s cwd-shadowing
  guard) — nothing in a request can choose what gets loaded. A client only talks to a socket
  whose directory is its own and not group/world accessible (``_private_dir``).
- ``TASK_CLASSIFY_SERVER=0`` disables the server entirely (classification stays in-process).
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

SERVER_ENV = "TASK_CLASSIFY_SERVER"
IDLE_SECONDS = 900.0
# How long a cold client waits for a spawned server that is still alive. Deliberately long: a
# live server is already paying the model load, and falling back to a SECOND in-process load
# would only compete with it for the same CPU/RAM (measured under a load average of ~36: a
# 20 s cap timed out and the fallback made the cold call 57 s instead of ~25 s).
_READY_TIMEOUT_S = 60.0
_REQUEST_TIMEOUT_S = 30.0
_MAX_REQUEST_BYTES = 4 * 1024 * 1024
_LOG_MAX_BYTES = 1024 * 1024


def enabled(env: dict[str, str] | None = None) -> bool:
    """Whether the warm server may be used on this machine (unix sockets + not opted out)."""
    env = os.environ if env is None else env
    if env.get(SERVER_ENV, "").strip().lower() in ("0", "off", "false", "no"):
        return False
    return hasattr(socket, "AF_UNIX")


def _state_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_STATE_HOME") or os.path.join(
        env.get("HOME", os.path.expanduser("~")), ".local", "state"
    )
    return Path(base) / "task-cli" / "classify"


def socket_path() -> Path:
    from . import __version__

    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in __version__)
    return _state_dir() / f"laya-{safe}.sock"


def request(sock_path: Path, message, questions, *, timeout: float, op: str = "predict"):
    """Send one request; return the server's ``result`` or ``None`` on any server-side error.

    Raises ``OSError`` when nothing is listening (so :func:`predict` can tell "spawn one" apart
    from "a live server answered with an error").
    """
    payload = json.dumps({"op": op, "message": message, "questions": questions}).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(str(sock_path))
        conn.sendall(payload)
        with conn.makefile("rb") as reader:
            line = reader.readline(_MAX_REQUEST_BYTES + 1)
    try:
        reply = json.loads(line)
    except ValueError:
        return None
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        return None
    return reply.get("result")


class StartError(Exception):
    """The server could not be started or never came up; the message says why."""


def predict(message: str, questions: dict):
    """The laya ``predict`` result for ``message`` from the warm server, or ``None``.

    Reuses a running server of any task-cli version (:func:`_running_socket`); otherwise starts
    one (:func:`_launch`) and waits up to ``_READY_TIMEOUT_S`` for it to load the model —
    returning early (``None``) when the server dies with an error, or when it lost the
    singleton race but no winner holds the lock any more. Never raises.
    """
    if not enabled():
        return None
    path = socket_path()
    if not _private_dir(path.parent):
        return None  # someone else could own or listen on this socket; never send them the text
    found = _running_socket(path)
    if found is not None:
        try:
            return request(found, message, questions, timeout=_REQUEST_TIMEOUT_S)
        except OSError:
            pass  # it went away between the probe and the request: start one
    try:
        proc, see = _launch(path)
        return _await_server(
            path,
            proc,
            lambda: request(_any_socket(path), message, questions, timeout=_REQUEST_TIMEOUT_S),
            _READY_TIMEOUT_S,
            see,
        )
    except StartError:
        return None  # the caller loads the model in-process instead


def start(wait_s: float = _READY_TIMEOUT_S) -> Path:
    """Make sure a warm server is accepting connections; return the socket it listens on.

    Idempotent: a running server of any task-cli version is reused (no spawn) and its socket
    returned — see :func:`_running_socket`. Otherwise starts one (:func:`_launch`: the installed
    unit, else a detached spawn) and waits up to ``wait_s`` for a socket to accept a connection,
    returning that socket. Concurrent callers are safe — the
    server's singleton flock lets one load the model, and a caller whose spawn lost that race
    keeps waiting for the winner. Raises :class:`StartError` naming the reason otherwise.
    """
    if not enabled():
        if not hasattr(socket, "AF_UNIX"):
            raise StartError("unix sockets are not available on this platform")
        raise StartError(f"the classify server is disabled ({SERVER_ENV}={os.environ[SERVER_ENV]})")
    path = socket_path()
    if not _private_dir(path.parent):
        raise StartError(f"{path.parent} must be a directory you own with mode 0700")
    found = _running_socket(path)
    if found is not None:
        return found
    proc, see = _launch(path)
    return _await_server(path, proc, lambda: _any_socket(path), wait_s, see)


def _any_socket(path: Path) -> Path:
    """:func:`_running_socket`, raising ``ConnectionRefusedError`` when none accepts yet.

    A unit may run a different installed task-cli version than this client, so the wait accepts
    whichever version's socket comes up, not only this version's ``path``.
    """
    found = _running_socket(path)
    if found is None:
        raise ConnectionRefusedError(f"no laya server accepts in {path.parent}")
    return found


def _launch(path: Path):
    """Start a server: ``(handle, where to look when it fails)``.

    The installed service-manager unit when there is one (:func:`_start_unit`), else a detached
    spawn of this version's server on ``path``. The handle has ``poll()`` like a ``Popen``.
    """
    unit = _start_unit(path)
    if unit is not None:
        return unit, unit.see
    return _spawn_checked(path), str(path.with_suffix(".log"))


UNIT_ENV = "TASK_CLASSIFY_UNIT"
DEFAULT_UNIT = "ai.hyperide.laya.service"
# A unit's state is checked at most this often while waiting (one manager call each).
_UNIT_POLL_INTERVAL_S = 1.0
_LAUNCHD_EXIT_RE = re.compile(r"last exit code = (\d+)")


class _StartedUnit:
    """A started unit, polled like a spawned ``Popen`` by :func:`_await_server`.

    ``state()`` returns ``"failed"`` (stopped with an error), ``"exited"`` (stopped cleanly) or
    ``None`` (still running, starting, or unknown).
    """

    def __init__(self, unit: str, state, see: str, sock_dir: Path) -> None:
        self.unit = unit
        self.see = see
        self._state = state
        self._sock_dir = sock_dir
        self._next_check = time.monotonic() + _UNIT_POLL_INTERVAL_S

    def poll(self) -> None:
        """``None`` while a server may still come up; raises :class:`StartError` once none will.

        A unit that stopped cleanly may have lost the singleton race to a server that is still
        loading (its lock is held): keep waiting for that one, like a lost-race spawn.
        """
        if time.monotonic() < self._next_check:
            return None
        self._next_check = time.monotonic() + _UNIT_POLL_INTERVAL_S
        state = self._state()
        if state == "failed":
            raise StartError(f"{self.unit} failed (see {self.see})")
        if state == "exited" and not _any_lock_held(self._sock_dir):
            raise StartError(f"{self.unit} exited before listening (see {self.see})")
        return None


def _any_lock_held(sock_dir: Path) -> bool:
    """Whether a server of any version holds its singleton lock in ``sock_dir``."""
    locks = sock_dir.glob("laya-*.sock.lock")
    return any(_lock_held(Path(str(lock).removesuffix(".lock"))) for lock in locks)


def _systemd_state(systemctl: str, unit: str) -> str | None:
    result = _run_manager([systemctl, "--user", "show", "-p", "ActiveState", "--value", unit])
    if result is None or result.returncode != 0:
        return None
    return {"failed": "failed", "inactive": "exited"}.get(result.stdout.strip())


def _launchd_state(launchctl: str, target: str) -> str | None:
    """Read ``launchctl print``'s ``state = ...`` and ``last exit code = N`` lines.

    Only the ``not running`` state counts as stopped, so a stale exit code from an earlier run
    is not mistaken for this run's while the kickstarted run is still being scheduled.
    """
    result = _run_manager([launchctl, "print", target])
    if result is None or result.returncode != 0 or "state = not running" not in result.stdout:
        return None
    code = _LAUNCHD_EXIT_RE.search(result.stdout)
    return "failed" if code is not None and code.group(1) != "0" else "exited"


def _account_home() -> str | None:
    """The user's home from the account database (what a service manager gives its units).

    For a uid with no passwd entry (e.g. a container's arbitrary uid) falls back to an absolute
    ``$HOME``, else ``None``: the home is unknown. Never raises, so :func:`predict` keeps its
    never-raises contract.
    """
    import pwd

    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        home = os.environ.get("HOME", "")
        return home if os.path.isabs(home) else None


def _systemd_manager_env(systemctl: str) -> dict[str, str] | None:
    """The user manager's environment (``systemctl --user show-environment``), plus ``HOME``
    from the account database when the manager does not set it; ``None`` when no home is
    known (then where the unit's server binds is unknown too)."""
    result = _run_manager([systemctl, "--user", "show-environment"])
    lines = result.stdout.splitlines() if result is not None and result.returncode == 0 else []
    env = dict(line.split("=", 1) for line in lines if "=" in line)
    if "HOME" not in env:
        home = _account_home()
        if home is None:
            return None
        env["HOME"] = home
    return env


def _same_dir(a: Path, b: Path) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _start_unit(path: Path) -> _StartedUnit | None:
    """Start the installed laya unit; ``None`` when no unit is installed or it can't serve us.

    Raises :class:`StartError` naming the unit when it is installed but will not start.
    ``path`` is this version's socket. The unit runs with the service manager's environment, not
    ours, so its server binds under the manager's state dir; when that is not ``path``'s dir (a
    caller-only ``XDG_STATE_HOME``/``HOME`` override) the unit could never answer here, and the
    caller spawns instead.
    """
    import shutil

    unit = os.environ.get(UNIT_ENV, "").strip() or DEFAULT_UNIT
    if sys.platform == "darwin":
        label = unit.removesuffix(".service")
        home = _account_home()
        launchctl = shutil.which("launchctl")
        if home is None or launchctl is None:
            return None
        plist = Path(home) / "Library" / "LaunchAgents" / f"{label}.plist"
        if not plist.is_file() or not _same_dir(_state_dir({"HOME": home}), path.parent):
            return None
        target = f"gui/{os.getuid()}/{label}"
        start_cmd = [launchctl, "kickstart", target]
        # A plist that exists but was never loaded (no login since it was written) makes
        # kickstart fail; then bootstrapping loads it and kickstart is retried once. A loaded
        # agent whose kickstart fails keeps that error (no bootstrap).
        load_cmd = [launchctl, "bootstrap", f"gui/{os.getuid()}", str(plist)]
        loaded_cmd = [launchctl, "print", target]  # exits non-zero for an agent not loaded
        started = _StartedUnit(
            label,
            lambda: _launchd_state(launchctl, target),
            f"launchctl print {target}",
            path.parent,
        )
    else:
        systemctl = shutil.which("systemctl")
        if systemctl is None:
            return None
        installed = _run_manager([systemctl, "--user", "cat", unit])
        if installed is None or installed.returncode != 0:
            return None
        manager_env = _systemd_manager_env(systemctl)
        if manager_env is None or not _same_dir(_state_dir(manager_env), path.parent):
            return None
        start_cmd = [systemctl, "--user", "start", unit]
        load_cmd = loaded_cmd = None  # `systemctl --user cat` succeeded: already loaded
        started = _StartedUnit(
            unit,
            lambda: _systemd_state(systemctl, unit),
            f"journalctl --user -u {unit}",
            path.parent,
        )
    result = _run_manager(start_cmd)
    if (
        result is not None
        and result.returncode != 0
        and load_cmd is not None
        and loaded_cmd is not None
        and not _ok(_run_manager(loaded_cmd))
    ):
        loaded = _run_manager(load_cmd)
        # A concurrent caller may have bootstrapped it first (ours then fails "already
        # loaded"): only a bootstrap failure that left the agent unloaded is an error.
        if not _ok(loaded) and not _ok(_run_manager(loaded_cmd)):
            raise StartError(_failure(load_cmd, loaded))
        result = _run_manager(start_cmd)
    if not _ok(result):
        raise StartError(_failure(start_cmd, result))
    return started


def _ok(result) -> bool:
    return result is not None and result.returncode == 0


def _failure(cmd: list[str], result) -> str:
    """One line naming ``cmd`` (and so the unit) and why it failed."""
    shown = " ".join([Path(cmd[0]).name, *cmd[1:]])
    if result is None:
        return f"`{shown}` could not run"
    detail = (result.stderr or "").strip().splitlines()
    reason = f": {detail[-1]}" if detail else ""
    return f"`{shown}` failed with status {result.returncode}{reason}"


def _run_manager(cmd: list[str]):
    """Run one service-manager command; the ``CompletedProcess``, or ``None`` if it can't run."""
    try:
        return subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_REQUEST_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _running_socket(path: Path) -> Path | None:
    """The first socket in ``path``'s dir that accepts a connection, or ``None``.

    Candidates are ``path`` and every ``laya-*.sock`` beside it, highest version first, and
    only real sockets we own (never a symlink). The caller has already checked the dir with
    :func:`_private_dir`.
    """
    candidates = {path, *path.parent.glob("laya-*.sock")}
    for candidate in sorted(filter(_own_socket, candidates), key=_version_key, reverse=True):
        try:
            _probe(candidate)
        except OSError:
            continue  # stale file of a server that died without cleanup
        return candidate
    return None


def _own_socket(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISSOCK(st.st_mode) and st.st_uid == os.getuid()


_SOCKET_VERSION_RE = re.compile(r"laya-(\d+(?:\.\d+)*)(.*)\.sock")


def _version_key(path: Path) -> tuple[tuple[int, ...], bool, str]:
    """Order sockets by the release segment of their version, numerically.

    ``laya-0.9.0.sock`` < ``laya-0.22.0.sock``. Only the leading ``\\d+(.\\d+)*`` counts, so a
    pre-release such as ``laya-0.23.1rc1.sock`` ties with ``laya-0.23.1.sock`` on release, and
    the final release wins the tie. A name with no version sorts lowest.
    """
    match = _SOCKET_VERSION_RE.fullmatch(path.name)
    if match is None:
        return (), False, path.name
    release = tuple(int(n) for n in match.group(1).split("."))
    return release, match.group(2) == "", path.name


def _probe(sock_path: Path) -> None:
    """Connect and hang up; raises ``OSError`` when nothing accepts on ``sock_path``."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(_REQUEST_TIMEOUT_S)
        conn.connect(str(sock_path))


def _spawn_checked(path: Path):
    """:func:`_spawn`, raising :class:`StartError` instead of returning ``None`` or raising."""
    try:
        proc = _spawn(path)
    except Exception as exc:
        raise StartError(f"could not spawn the server: {exc}") from exc
    if proc is None:
        raise StartError(f"{sys.executable} does not support -P, which the server needs")
    return proc


def _await_server(path: Path, proc, attempt, timeout_s: float, see: str):
    """Return ``attempt()`` once it stops raising ``OSError`` (i.e. the started server listens).

    Raises :class:`StartError` early when ``proc`` died with an error, or exited cleanly after
    losing the singleton race while no winner holds the lock any more (e.g. the winner's model
    load failed): nothing will ever listen. ``proc.poll()`` may itself raise :class:`StartError`
    (a failed unit). Otherwise gives up after ``timeout_s``. ``see`` is where the server's own
    output is (its log file, or the service manager's log command).
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return attempt()
        except OSError:
            pass
        rc = proc.poll()
        if rc is not None and rc != 0:
            raise StartError(f"the server exited with status {rc} (see {see})")
        if rc == 0 and not _lock_held(path):
            raise StartError(f"the server exited before listening (see {see})")
        if time.monotonic() >= deadline:
            raise StartError(
                f"the server did not accept connections within {timeout_s:g} s (see {see})"
            )
        time.sleep(0.05)


def _private_dir(directory: Path) -> bool:
    """Whether ``directory`` is absent (``_spawn`` creates it 0700) or ours and not group/world
    accessible — so no other user can have planted a listener that answers as the model."""
    try:
        st = os.lstat(directory)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077


def _lock_held(sock_path: Path) -> bool:
    """Whether some server process currently holds the singleton lock for ``sock_path``."""
    import fcntl

    try:
        fd = os.open(str(sock_path) + ".lock", os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)  # closing drops the lock if we just took it
    return False


def _spawn(path: Path):
    """Start the server detached; the ``Popen`` handle, or ``None`` when it can't be started."""
    from . import daemon

    if not daemon.python_supports_safepath():
        return None  # -P is required to keep a cwd `tasklib/` from shadowing the real package
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    log_path = path.with_suffix(".log")
    try:
        if log_path.stat().st_size > _LOG_MAX_BYTES:
            log_path.unlink()
    except OSError:
        pass
    repo_root = daemon._repo_root()
    env = (
        daemon._child_env(repo_root)
        if daemon._needs_pythonpath_bootstrap(repo_root)
        else daemon._sanitized_child_env()
    )
    argv = [sys.executable, "-P", "-m", "tasklib.classify_server", str(path)]
    with open(log_path, "a", encoding="utf-8") as logf:
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            start_new_session=True,
            cwd=str(path.parent),
            env=env,
        )


def serve(sock_path: Path, predictor, *, idle_s: float = IDLE_SECONDS, load=None) -> int:
    """Serve ``predictor(message, questions)`` on ``sock_path`` until idle for ``idle_s``.

    ``load`` (optional) runs AFTER the singleton lock is held and BEFORE the socket binds, so a
    client never connects to a server whose model isn't loaded yet, and a losing racer never
    loads a second copy. Returns 0 on an idle/requested exit or when another server already
    holds the lock.
    """
    import fcntl

    sock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(sock_path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # another server owns this socket
        if load is not None:
            predictor = load()
        try:
            sock_path.unlink()
        except FileNotFoundError:
            pass
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
            old_umask = os.umask(0o177)
            try:
                srv.bind(str(sock_path))
            finally:
                os.umask(old_umask)
            os.chmod(sock_path, 0o600)
            srv.listen(16)
            srv.settimeout(idle_s)
            try:
                while True:
                    try:
                        conn, _ = srv.accept()
                    except socket.timeout:
                        return 0
                    with conn:
                        if _handle(conn, predictor) == "shutdown":
                            return 0
            finally:
                try:
                    sock_path.unlink()
                except FileNotFoundError:
                    pass
    finally:
        os.close(lock_fd)


def _handle(conn: socket.socket, predictor) -> str | None:
    conn.settimeout(_REQUEST_TIMEOUT_S)
    try:
        with conn.makefile("rb") as reader:
            line = reader.readline(_MAX_REQUEST_BYTES + 1)
        req = json.loads(line)
        if not isinstance(req, dict):
            raise ValueError("request is not a JSON object")
        if req.get("op") == "shutdown":
            _reply(conn, _encode({"ok": True, "result": None}))
            return "shutdown"
        message, questions = req.get("message"), req.get("questions")
        if not isinstance(message, str) or not isinstance(questions, dict):
            raise ValueError("request needs a string message and a questions object")
        payload = _encode({"ok": True, "result": predictor(message, questions)})
    except Exception as exc:  # one bad request (or unserializable result) never kills the server
        payload = _encode({"ok": False, "error": type(exc).__name__})
    _reply(conn, payload)
    return None


def _encode(reply: dict) -> bytes:
    return json.dumps(reply).encode() + b"\n"


def _reply(conn: socket.socket, payload: bytes) -> None:
    try:
        conn.sendall(payload)
    except OSError:
        pass  # the client went away; nothing to do


def _load_laya_predictor():
    from .classifiers import LayaClassifier

    provider = LayaClassifier(use_server=False)
    if not provider.available():
        raise SystemExit("laya is not installed; nothing to serve")
    agent = provider._load_agent()
    return agent.predict


def run_server(path: Path | None = None) -> int:
    """Serve the laya model on ``path`` (default: this version's socket) in the foreground.

    ``task classify-server serve`` — what the service-manager unit's ExecStart runs — and the
    detached spawn both end here. Creates the socket dir 0700 and moves into it (the
    cwd-shadowing guard, see the module docstring), then :func:`serve` with the singleton flock
    and the :data:`IDLE_SECONDS` idle exit. Returns 0 on an idle exit.
    """
    path = socket_path() if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    os.chdir(path.parent)
    return serve(path, None, idle_s=IDLE_SECONDS, load=_load_laya_predictor)


def main(argv: list[str]) -> int:
    from . import daemon

    daemon._scrub_bootstrap_pythonpath()
    return run_server(Path(argv[0]) if argv else None)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
