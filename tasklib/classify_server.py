"""A warm, machine-wide laya classify server so `task classify` stops reloading the model.

WHY (task-cli#245): loading the laya checkpoint costs ~3 s of every `task classify` call
(measured: torch import 0.5 s, `laya.load` 2.8 s, `predict` 0.1 s), and each call is a fresh
process, so the tg-cli inbound hook and grooming passes paid 4-11 s per message. This module
keeps ONE loaded model resident in a detached process and answers `predict` requests over a
unix socket, so a warm call is one socket round-trip.

HOW it is reached: `LayaClassifier.classify` calls :func:`predict`. On the first call no server
is listening, so :func:`predict` spawns ``python -P -m tasklib.classify_server`` detached (the
same interpreter/env bootstrap `task daemon start` uses — `daemon._child_env` etc.) and waits
until the socket accepts; a later call just connects. Every failure (spawn refused, server died,
socket error, bad reply) returns ``None`` and the caller loads the model in-process exactly as
before — the server is an accelerator, never a new way for classify to fail.

``task classify-server start`` (:func:`start`, task-cli#279) is the same spawn-and-wait for a
caller that NEEDS the server (text-lint, where laya is mandatory): it returns the socket path once
the socket accepts, or raises :class:`StartError` with the reason instead of falling back.

INVARIANTS:
- One server per (user, task-cli version): the socket lives under the user's state dir
  (``$XDG_STATE_HOME/task-cli/classify/``, mode 0700; the socket itself 0600) and its name
  carries ``tasklib.__version__``, so an upgraded client never talks to an old server — the old
  one simply idles out.
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

    Spawns the server on first use and waits up to ``_READY_TIMEOUT_S`` for it to load the
    model — returning early (``None``) when the spawned server dies with an error, or when it
    lost the singleton race but no winner holds the lock any more. Never raises.
    """
    if not enabled():
        return None
    path = socket_path()
    if not _private_dir(path.parent):
        return None  # someone else could own or listen on this socket; never send them the text
    try:
        return request(path, message, questions, timeout=_REQUEST_TIMEOUT_S)
    except OSError:
        pass
    try:
        proc = _spawn_checked(path)
        return _await_server(
            path,
            proc,
            lambda: request(path, message, questions, timeout=_REQUEST_TIMEOUT_S),
            _READY_TIMEOUT_S,
        )
    except StartError:
        return None  # the caller loads the model in-process instead


def start(wait_s: float = _READY_TIMEOUT_S) -> Path:
    """Make sure the warm server is accepting connections; return its socket path.

    Idempotent: a server already accepting is left alone (no spawn). Otherwise spawns one and
    waits up to ``wait_s`` for its socket to accept a connection. Concurrent callers are safe —
    the server's singleton flock lets one load the model, and a caller whose spawn lost that
    race keeps waiting for the winner. Raises :class:`StartError` naming the reason otherwise.
    """
    if not enabled():
        if not hasattr(socket, "AF_UNIX"):
            raise StartError("unix sockets are not available on this platform")
        raise StartError(f"the classify server is disabled ({SERVER_ENV}={os.environ[SERVER_ENV]})")
    path = socket_path()
    if not _private_dir(path.parent):
        raise StartError(f"{path.parent} must be a directory you own with mode 0700")
    try:
        _probe(path)
        return path
    except OSError:
        pass
    _await_server(path, _spawn_checked(path), lambda: _probe(path), wait_s)
    return path


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


def _await_server(path: Path, proc, attempt, timeout_s: float):
    """Return ``attempt()`` once it stops raising ``OSError`` (i.e. the spawned server listens).

    Raises :class:`StartError` early when ``proc`` died with an error, or exited cleanly after
    losing the singleton race while no winner holds the lock any more (e.g. the winner's model
    load failed): nothing will ever listen. Otherwise gives up after ``timeout_s``.
    """
    log = path.with_suffix(".log")
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return attempt()
        except OSError:
            pass
        rc = proc.poll()
        if rc is not None and rc != 0:
            raise StartError(f"the server exited with status {rc} (see {log})")
        if rc == 0 and not _lock_held(path):
            raise StartError(f"the server exited before listening (see {log})")
        if time.monotonic() >= deadline:
            raise StartError(
                f"the server did not accept connections within {timeout_s:g} s (see {log})"
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


def main(argv: list[str]) -> int:
    from . import daemon

    daemon._scrub_bootstrap_pythonpath()
    path = Path(argv[0]) if argv else socket_path()
    return serve(path, None, load=_load_laya_predictor)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
