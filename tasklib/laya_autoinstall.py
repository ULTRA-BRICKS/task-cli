"""Runtime auto-install of the optional `laya` PyPI package (task-cli#208).

WHAT/WHY: task-cli#206 made `laya` (Convai Innovations' free, local, Apache-2.0 classifier --
see `tasklib/classifiers.py`) a pluggable `ClassifierProvider`, and task-cli#208 makes it the
DEFAULT zero-config `classify.fallbacks` head (`config.DEFAULTS["classify"]["fallbacks"]`). A
repo that never explicitly opted into `laya` should still get to use it the FIRST time `task
classify` runs, without a manual `uv sync --extra laya` / `pip install task-cli[laya]` step --
this module is that bridge. `LayaClassifier.available()` (classifiers.py) calls
:func:`ensure_installed` exactly once per instance, on the specific "package genuinely not
importable yet" path (never on a model-load/predict failure -- that's a separate, unrelated
runtime-failure path handled entirely inside `LayaClassifier.classify()`), before falling
through to the next provider in the chain.

Never a hard/eager dependency of anything: this module itself is stdlib-only (no `laya`
import anywhere in it), and is only ever imported lazily from inside
`LayaClassifier.available()` -- `task --help` and every classify call that never reaches the
laya branch (an explicit non-laya `classify.fallbacks`) never import this module at all.

ALWAYS `sys.executable`: `install()` takes no interpreter argument -- it always targets
`sys.executable`, the CURRENTLY RUNNING interpreter, which is guaranteed to be the same one
future `task classify` invocations run under (same `env python3` shebang resolution). This
also means `_in_virtualenv()`'s check (`sys.prefix != sys.base_prefix`) always correctly
describes the actual pip target, by construction -- an earlier revision accepted an arbitrary
`python_exe` parameter while still checking the CURRENT process's venv-ness, which could
diverge if ever called with a different interpreter (review finding); removing the parameter
removes that whole bug class rather than trying to keep the two in sync.

VERSION PIN: installs the SAME `laya>=0.3.7,<0.4` spec as `pyproject.toml`'s `laya` extra
(`_LAYA_PIP_SPEC`) -- `tasklib/classifiers.py`'s `LayaClassifier._map_result` hand-parses
`Agent.predict()`'s raw dict envelope, which isn't a versioned/stable public contract yet (see
`pyproject.toml`'s own comment); an unpinned install could silently pull a breaking `laya`
release that this module can no longer parse, permanently degrading the zero-config default
with no way to self-heal (a "successful" but envelope-incompatible install still passes
`find_spec`, so it would never retry). `pyproject.toml`, this module, and `scripts/lib/
install_laya.sh` each hold their OWN copy of the spec string (a single cross-language constant
isn't practical across TOML/Python/bash) -- `tests/test_laya_autoinstall.py` asserts this
module's copy matches `pyproject.toml`'s via `tomllib`, so a drift is a test failure, not a
silent divergence.

VIRTUALENV: `pip install --user` is UNCONDITIONALLY REFUSED inside an activated virtualenv
("User site-packages are not visible in this virtualenv") -- confirmed live (this exact error,
against a real venv, during this ticket's own end-to-end verification). `--user` is dropped
(never passed) when `sys.prefix != sys.base_prefix` (the standard stdlib venv-detection check):
a venv's own site-packages directory is ALREADY an isolated, user-writable location, so a plain
`pip install laya` there is the correct equivalent of `--user` outside one -- no `--break-
system-packages` retry is meaningful inside a venv either (PEP 668's externally-managed-
environment guard is a SYSTEM-Python concept; a venv is never "externally managed"). For the
SAME reason, `_refresh_user_site_path` (below) is a no-op inside a venv: nothing was installed
into the OS user-site directory there, and appending it to the venv's `sys.path` would only
pollute an isolated environment with unrelated host packages/`.pth` side effects.

PEP 668 ("externally-managed-environment"): `bin/task`'s `#!/usr/bin/env python3` shebang
resolves, on a Homebrew/Linux-distro-managed Python, to an environment that refuses a plain
`pip install --user laya` outright (verified live on the dev machine this ticket was built
on). The fix pip's OWN error message recommends is `--break-system-packages` -- but that flag
is only ever appended on a SECOND, PEP-668-specific retry (exit code 1 AND
`externally-managed-environment` present in stderr), never as a blanket first attempt and
never as a blanket retry-on-any-failure: an unrelated failure (no network, disk full, a
genuinely broken pip) must not gain a second, noisier attempt that would just fail the
identical way and add nothing but log noise. `--user` (present on non-venv attempts) confines
the install to the invoking user's OWN site-packages -- never a Homebrew-managed or
system-wide file -- which is what makes auto-appending `--break-system-packages` safe to do
without a human in the loop: the flag only ever widens permission to write into a directory the
user (or, inside a venv, the venv itself) already owns, never anywhere else.

COOLDOWN CACHE: a failed attempt (offline machine, broken pip, disk full, a timed-out
download, OR a pip-reported "success" that still leaves `laya` unimportable) is cached under
`$XDG_STATE_HOME/task-cli/laya_autoinstall` (mirrors `stale_notice.py`'s own per-feature
cache-file convention under the same state root) so a SUBSEQUENT `task classify` within the
cooldown window skips the network attempt entirely and falls straight through to the
configured fallback chain -- an offline machine must never pay a pip-timeout tax on every
single classify call, and a "succeeds but unimportable" machine must not re-run a no-op `pip
install` (and re-print the status line) on every single invocation either. `ensure_installed`
marks failure in BOTH cases -- a nonzero pip exit AND a zero exit whose package still doesn't
`find_spec`. 24h (`_COOLDOWN_S_DEFAULT` / `$TASK_LAYA_AUTOINSTALL_COOLDOWN_S`) is long enough
that a genuinely offline (or genuinely broken) machine isn't retried on every invocation, short
enough that a machine reconnected/fixed overnight tries again the next morning with no manual
nudge required. A GENUINELY successful, importable install writes no marker at all: the next
`available()` call finds the package importable via a plain, cheap `find_spec` check and never
calls this module again for the lifetime of that install. The marker itself is written
atomically (temp file + `os.replace`, mirroring `daemon.py`'s `save_notified`) so a concurrent
`task classify` reading it mid-write can only ever see a complete file or the prior one, never
a truncated one.

FAIL-OPEN, matching `stale_notice.py`'s own NaN/non-dict-cache handling: an absent, corrupted,
or unreadable cache file all read back as "never attempted" (0.0), so a broken/hand-edited
marker can only ever cause an EXTRA attempt, never a permanent "auto-install is now wedged
off" state.

TIMEOUT: `_PIP_TIMEOUT_S` bounds each `pip install` subprocess so a hung/stalled download can
never hang `task classify` itself -- installing a small pure-Python wheel normally takes
seconds; a few minutes is generous headroom without risking an unbounded hang.

TEST HERMETICITY: `tests/conftest.py`'s autouse `_no_real_laya_autoinstall` fixture
monkeypatches :func:`ensure_installed` to `False` for every test EXCEPT this module's own
(`tests/test_laya_autoinstall.py`) and `tasklib/classifiers.py`'s wiring tests
(`tests/test_classifiers.py`, which mock `ensure_installed` themselves anyway) -- see that
fixture's docstring for why a real, unmocked classify test must never risk a real
subprocess/network call or a real package install into the machine running the suite.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

_COOLDOWN_S_DEFAULT = 24 * 60 * 60  # 24h -- see module docstring's COOLDOWN CACHE note
_PIP_TIMEOUT_S = 180  # 3 minutes per pip subprocess call -- see module docstring's TIMEOUT note
_MAX_DETAIL_CHARS = 200  # truncate a printed pip-stderr detail so one failure can't flood stderr

# The spec `pyproject.toml`'s `laya` extra pins -- see module docstring's VERSION PIN note.
# `tests/test_laya_autoinstall.py` asserts this matches `pyproject.toml` via `tomllib`.
_LAYA_PIP_SPEC = "laya>=0.3.7,<0.4"


def _in_virtualenv() -> bool:
    """The standard stdlib venv-detection check for the CURRENT process -- `install()` always
    targets `sys.executable`, so this always correctly describes its actual pip target (see
    module docstring's `ALWAYS sys.executable` note)."""
    return sys.prefix != sys.base_prefix


def _env_seconds(name: str, default: int) -> int:
    """An int from an env var, falling back to `default` on absent/garbage/non-positive input.

    Mirrors `stale_notice._env_seconds`, kept as its own small copy rather than a shared
    import -- the two modules' cache semantics are independent and this is too small to be
    worth a cross-module dependency. Unlike `stale_notice`'s version, non-positive values also
    fall back to `default` here (there is no "0 = disabled" mode for this cooldown), which is
    what `cooldown_seconds` needs -- keeping the whole non-positive-rejection in this ONE spot
    avoids a second, redundant clamp downstream.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def cooldown_seconds() -> int:
    """The resolved cooldown window (`$TASK_LAYA_AUTOINSTALL_COOLDOWN_S` override, else 24h)."""
    return _env_seconds("TASK_LAYA_AUTOINSTALL_COOLDOWN_S", _COOLDOWN_S_DEFAULT)


def _state_dir(env: dict[str, str] | None = None) -> Path:
    """The base dir for this feature's cache file (`$XDG_STATE_HOME` -> `~/.local/state`),
    the same base `session.py`/`read_tracker.py`/`stale_notice.py`/`daemon.py` all share."""
    env = os.environ if env is None else env
    base = env.get("XDG_STATE_HOME") or os.path.join(
        env.get("HOME", os.path.expanduser("~")), ".local", "state"
    )
    return Path(base) / "task-cli" / "laya_autoinstall"


def cache_path(env: dict[str, str] | None = None) -> Path:
    """Where the last FAILED attempt's timestamp is recorded (no marker on a genuinely
    successful, importable install -- discovered directly via `find_spec`, see module
    docstring's COOLDOWN CACHE note)."""
    return _state_dir(env) / "last_failed_attempt.json"


def _read_last_attempt(path: Path) -> float:
    """The recorded last-failed-attempt epoch, or 0 for absent/malformed/non-finite cache
    content -- mirrors `stale_notice._read_last_checked`'s fail-open handling exactly (see
    module docstring's FAIL-OPEN note); the future-timestamp guard lives in `should_attempt`.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0
    if not isinstance(data, dict):
        return 0.0
    try:
        value = float(data.get("last_failed_attempt_at", 0))
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _write_last_attempt(path: Path, ts: float) -> None:
    """Best-effort, ATOMIC write (temp file + `os.replace`, mirrors `daemon.save_notified`) --
    a concurrent `should_attempt` reader can only ever see a complete file or the prior one,
    never a truncated/torn one. A write failure is swallowed (mirrors `read_tracker.record`):
    the next `should_attempt` just sees no marker and retries sooner than the cooldown
    intends -- never a crash, never a reason to fail the `task classify` call this serves.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"last_failed_attempt_at": ts}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def should_attempt(*, now: float | None = None, env: dict[str, str] | None = None) -> bool:
    """Whether the cooldown has elapsed since the last FAILED install attempt (or none has
    ever been recorded). Read-only -- does not itself record anything."""
    now = time.time() if now is None else now
    elapsed = now - _read_last_attempt(cache_path(env))
    # A corrupted/clock-skewed cache recording a FUTURE timestamp must not permanently
    # suppress retrying (mirrors `stale_notice.should_check`'s identical guard) -- treat a
    # negative elapsed as due instead of wedging this shut forever.
    if elapsed < 0:
        return True
    return elapsed >= cooldown_seconds()


def mark_failed_attempt(*, now: float | None = None, env: dict[str, str] | None = None) -> None:
    """Record that an install attempt just failed (pip itself failed, OR pip reported success
    but the package still isn't importable -- see module docstring's COOLDOWN CACHE note),
    right now."""
    now = time.time() if now is None else now
    _write_last_attempt(cache_path(env), now)


def _truncate(text: str) -> str:
    text = text.strip().replace("\n", " ")
    return text if len(text) <= _MAX_DETAIL_CHARS else text[:_MAX_DETAIL_CHARS] + "…"


def _run_pip(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess | None:
    """`subprocess.run(argv, ...)`, folding a timeout / missing-interpreter / any other
    `OSError` into `None` rather than raising -- the caller gets one uniform "this attempt
    didn't complete" signal regardless of which of those actually happened. Also catches
    `UnicodeDecodeError` (a `ValueError` subclass): `text=True` decodes pip's output with the
    locale codec, which can raise on non-decodable bytes on a `LC_ALL=C`/POSIX-locale machine
    -- an undecodable byte in pip's own output must not escape as an uncaught exception and
    silently skip the cooldown marker this function exists to let the caller record.
    """
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def _pip_failure_detail(result: subprocess.CompletedProcess) -> str:
    return _truncate(result.stderr or f"pip exited {result.returncode}")


def install(*, timeout: float = _PIP_TIMEOUT_S) -> tuple[bool, str]:
    """Attempt `pip install <_LAYA_PIP_SPEC>` under `sys.executable` (see module docstring's
    `ALWAYS sys.executable` note), retrying ONCE with `--break-system-packages` appended ONLY
    when that first attempt fails with exit code 1 AND `externally-managed-environment` in
    stderr (see module docstring's PEP 668 note). `--user` is included UNLESS the current
    process is a virtualenv (see module docstring's VIRTUALENV note), where it is dropped
    entirely and the PEP-668 retry never applies either. Never raises.

    Returns `(success, detail)`: on success `detail` is a short human-readable note
    ("installed" / "installed (--break-system-packages)"); on failure it is a truncated,
    single-line tail of pip's stderr (or a timeout/could-not-run note).
    """
    prefix = [sys.executable, "-m", "pip", "install"]
    if not _in_virtualenv():
        prefix.append("--user")
    result = _run_pip([*prefix, _LAYA_PIP_SPEC], timeout=timeout)
    if result is None:
        return False, "pip did not complete (timeout or could not run)"
    if result.returncode == 0:
        return True, "installed"
    if result.returncode == 1 and "externally-managed-environment" in (result.stderr or ""):
        retry = _run_pip([*prefix, "--break-system-packages", _LAYA_PIP_SPEC], timeout=timeout)
        if retry is None:
            return False, "pip did not complete (timeout or could not run, retry)"
        if retry.returncode == 0:
            return True, "installed (--break-system-packages)"
        return False, _pip_failure_detail(retry)
    return False, _pip_failure_detail(result)


def _laya_importable() -> bool:
    """`importlib.util.find_spec("laya") is not None`, never raising -- the same "is the
    package importable right now" probe `LayaClassifier.available()` uses, duplicated here
    (not imported from classifiers.py) to keep this module's only dependency on the standard
    library, matching its stdlib-only-at-import-time contract."""
    try:
        return importlib.util.find_spec("laya") is not None
    except Exception:
        return False


def _refresh_user_site_path() -> None:
    """Make a FIRST-EVER `--user` install visible to THIS already-running process.

    `importlib.invalidate_caches()` alone is not enough (live-verified): the user
    site-packages directory (`site.getusersitepackages()`) is only present on `sys.path` at
    interpreter STARTUP if it already existed at that point -- a brand-new machine/`$HOME`
    where that directory did not exist yet (so `pip install --user` had to create it) leaves
    it absent from `sys.path` for the REST of this process's lifetime, no matter how many
    times import caches are invalidated. `site.addsitedir()` adds it explicitly, regardless of
    `site.ENABLE_USER_SITE` (irrelevant here -- this call is unconditional-within-the-non-venv-
    branch because THIS process just explicitly `--user`-installed into exactly that directory
    and unambiguously wants to use what it put there). A no-op inside a virtualenv (`install()`
    never passes `--user` there -- see module docstring's VIRTUALENV note): nothing was
    installed into the OS user-site directory, so adding it would only pollute the venv's
    `sys.path` with unrelated host packages. Never raises: `site` is stdlib and this is a pure
    `sys.path`/`.pth` bookkeeping call.
    """
    if _in_virtualenv():
        return
    import site

    try:
        site.addsitedir(site.getusersitepackages())
    except Exception:
        pass


def ensure_installed(*, env: dict[str, str] | None = None, now: float | None = None) -> bool:
    """The single entry point `LayaClassifier.available()` calls on the "package not
    importable yet" path.

    Skips silently (no subprocess, no output) when the cooldown hasn't elapsed since the last
    failed attempt. Otherwise prints ONE brief status line to stderr, attempts the install
    (against `sys.executable` -- the SAME interpreter future `task classify` invocations also
    run under, so a success here is guaranteed importable next time too), refreshes `sys.path`
    for a first-ever install (`_refresh_user_site_path`) and `importlib.invalidate_caches()`,
    then re-verifies importability before declaring success -- a pip exit-0 does not, by
    itself, guarantee `import laya` will now work (e.g. a genuinely broken/incompatible wheel).
    Either kind of failure (pip itself failing, or pip succeeding but the package still not
    importable) records the SAME cooldown marker, so a persistently "succeeds but unimportable"
    machine doesn't re-run a no-op `pip install` (and re-print this status line) on every
    invocation.

    Never raises (mirrors `LayaClassifier.available()`'s own "never raises" contract, which
    this function is the one place that could otherwise violate via a `print()` to a closed
    stderr) -- any unexpected exception ALSO marks the cooldown before degrading to `False`,
    so a weird/unexpected failure doesn't bypass the cooldown and re-attempt on every call.

    Returns whether `laya` is now genuinely importable in THIS process.
    """
    try:
        if not should_attempt(now=now, env=env):
            return False
        print("laya not installed; installing now...", file=sys.stderr)
        ok, detail = install()
        if ok:
            _refresh_user_site_path()
            importlib.invalidate_caches()
            if _laya_importable():
                print(f"  laya {detail}", file=sys.stderr)
                return True
            detail = f"{detail}, but the package is still not importable"
        print(f"  laya install failed ({detail}); falling back to next provider", file=sys.stderr)
        mark_failed_attempt(now=now, env=env)
        return False
    except Exception:
        mark_failed_attempt(now=now, env=env)
        return False
