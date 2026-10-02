"""Runtime auto-install of the optional `laya` PyPI package (task-cli#208, task-cli#224).

WHAT/WHY: task-cli#206 made `laya` (Convai Innovations' free, local, Apache-2.0 classifier --
see `tasklib/classifiers.py`) a pluggable `ClassifierProvider`, and task-cli#208 makes it the
DEFAULT zero-config `classify.fallbacks` head (`config.DEFAULTS["classify"]["fallbacks"]`). A
repo that never explicitly opted into `laya` should still get to use it the FIRST time `task
classify` runs, without a manual `uv sync --extra laya` / `pip install hyper-task[laya]` step --
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
describes the actual pip target, by construction, AND means a freshly-created dedicated venv
(below) is always built to match `sys.executable`'s exact ABI/version -- an earlier revision
accepted an arbitrary `python_exe` parameter while still checking the CURRENT process's
venv-ness, which could diverge if ever called with a different interpreter (review finding);
removing the parameter removes that whole bug class rather than trying to keep the two in
sync.

VERSION PIN: installs the SAME `laya>=0.3.7,<0.4` spec as `pyproject.toml`'s `laya` extra
(`_LAYA_PIP_SPEC`) -- `tasklib/classifiers.py`'s `LayaClassifier._map_result` hand-parses
`Agent.predict()`'s raw dict envelope, which isn't a versioned/stable public contract yet (see
`pyproject.toml`'s own comment); an unpinned install could silently pull a breaking `laya`
release that this module can no longer parse, permanently degrading the zero-config default
with no way to self-heal (a "successful" but envelope-incompatible install still passes
`find_spec`, so it would never retry). `pyproject.toml` and this module each hold their OWN
copy of the spec string (a single cross-language constant isn't practical across TOML/Python)
-- `tests/test_laya_autoinstall.py` asserts this module's copy matches `pyproject.toml`'s via
`tomllib`, so a drift is a test failure, not a silent divergence. `scripts/lib/install_laya.sh`
no longer holds its own copy at all (see DELEGATION note below) -- it now calls straight into
this module's `install()`, so there is exactly one Python-side spec plus `pyproject.toml`.

DEDICATED VENV, not an escalating pip flag (task-cli#224): PEP 668's
"externally-managed-environment" guard is a property of a SYSTEM/Homebrew/distro-managed
Python's OWN site-packages -- it fires because pip refuses to write into a location it doesn't
consider safely user-owned. That guard is checked against the RUNNING interpreter's own
`sys.prefix`; it never fires against pip invoked from inside a virtualenv, because a venv's
site-packages is *always* a safely user-owned, isolated location by construction. A flag like
pip's `--break-system-packages` "fixes" the symptom by asking pip to bypass its own safety
check in place, on the very Python installation that check exists to protect -- exactly the
kind of shortcut banned outright for any auto-install/bootstrap path in this project (project
directive, task-cli#224): it widens permission on a shared system resource rather than
avoiding touching it. This module instead sidesteps the guard entirely by never running pip
against a PEP-668-managed interpreter in the first place:

  1. Already inside a virtualenv (`_in_virtualenv()` True, i.e. THIS process's own
     `sys.executable` -- see `ALWAYS sys.executable` above): install straight into it. A
     venv's site-packages is already an isolated, user-writable location, so a plain install
     (no `--user`, no retry branch) is unconditionally safe and immediately importable --
     nothing further is needed. It runs `uv pip install --python <sys.executable> <spec>`
     when a trusted `uv` resolves (a `uv tool install hyper-task` tool venv has no pip), else
     `<sys.executable> -m pip install <spec>`.
  2. Not inside a virtualenv (a system/Homebrew/pyenv/distro Python that may well be
     PEP-668-managed): install into a SEPARATE, DEDICATED, persistent venv this module owns
     and manages (`_dedicated_venv_dir`, a `laya_venv-py<major>.<minor>` directory sibling to
     the cooldown-cache file under the same `$XDG_STATE_HOME`-rooted state dir -- see
     `_state_dir`). The directory name is keyed by `sys.version_info` (review finding): a
     later invocation running under a DIFFERENT interpreter (a `$PATH` reorder, a pyenv shim
     swap, a second Python install) must never reuse a venv built for a different Python
     version -- that would bolt incompatible compiled wheels onto the wrong interpreter's
     `sys.path` (see `_activate_venv_site` below), silently breaking `import laya` forever.

     Created once (`uv venv --seed --python <sys.executable> <dir>` when `uv` is on `$PATH`
     -- fast, matches this project's own tooling convention; else `sys.executable -m venv
     --clear <dir>`, always available, stdlib, and still guarantees the exact same
     interpreter/ABI since it IS `sys.executable` cloning itself) and reused on every
     subsequent call (`_ensure_dedicated_venv` only (re)creates it when a `-c "import sys"`
     sanity probe against its own interpreter fails -- a corrupt venv self-heals by
     rebuilding rather than permanently wedging the feature off). `--seed` on the `uv venv`
     path (review finding) is NOT optional: `uv venv` alone creates a venv with no pip in it
     at all (`uv pip install` doesn't need one), but the FALLBACK installer below does; if
     `uv` is present at creation time and later disappears from `$PATH` (uninstalled, a
     different shell/cron environment), an unseeded venv would have no pip and no way to
     self-heal, since the sanity probe only checks the interpreter runs, not that pip exists.
     Seeding makes both creation paths interchangeable from the fallback installer's point of
     view. `uv` is resolved ONCE per `install()` call and threaded through to both the
     creation and the install step, so the two can never disagree about whether it's
     available.

     `laya` installs into THAT venv's own interpreter (`uv pip install --python <venv_python>
     <spec>` when `uv` is available, else `<venv_python> -m pip install <spec>` using the
     venv's own bundled pip) -- never externally managed, so this is safe unconditionally,
     with no retry branch of any kind.

     A successful install into the dedicated venv is NOT automatically importable in this
     already-running process (it lives on a completely different `sys.path` than the ambient
     interpreter's). `_venv_site_packages` asks the venv's OWN python where its site-packages
     actually live (`sysconfig.get_path("purelib")`, queried via a subprocess against that
     interpreter -- correct across platforms/versions without hand-computing
     `lib/pythonX.Y/site-packages`), and `_activate_venv_site` calls `site.addsitedir()` on
     THIS process with that path -- the exact same "a brand-new directory isn't on `sys.path`
     at interpreter startup" fix the old `--user`-install era used. The resolved path is ALSO
     cached in a small marker file inside the venv directory itself (`_venv_site_marker_path`
     -- self-heals for free: an `--clear` rebuild wipes it along with everything else). A
     later, fresh process tries that marker FIRST (`_try_activate_cached_dedicated_venv`,
     zero subprocesses) before ever calling `install()` -- review finding: without this, every
     single fresh process would re-spawn the probe/pip/sysconfig subprocesses (and re-print
     the install status line) forever, which directly contradicted this module's own COOLDOWN
     CACHE rationale below for why a success marker matters at all.

DELEGATION: `scripts/lib/install_laya.sh` (best-effort, run by `install.sh` when `task` itself
is installed) no longer hand-duplicates this logic in bash -- it shells out to THIS module's
`install()` via `PYTHONPATH=<repo root> <python_exe> -c "from tasklib.laya_autoinstall import
install; ..."`, so there is exactly one implementation of the install algorithm, never two
copies that can drift (a duplicate-spec-cross-check test used to exist specifically to catch
that drift; it is gone now because there is nothing left to drift).

COOLDOWN CACHE: a failed attempt (offline machine, broken pip, disk full, a timed-out
download, a venv that fails to create/self-heal, OR a "success" that still leaves `laya`
unimportable) is cached under `$XDG_STATE_HOME/task-cli/laya_autoinstall` (mirrors
`stale_notice.py`'s own per-feature cache-file convention under the same state root) so a
SUBSEQUENT `task classify` within the cooldown window skips the network attempt entirely and
falls straight through to the configured fallback chain -- an offline machine must never pay a
pip-timeout tax on every single classify call, and a "succeeds but unimportable" machine must
not re-run a no-op `pip install` (and re-print the status line) on every single invocation
either. `ensure_installed` marks failure in BOTH cases -- `install()` itself failing AND a
`True` result whose package still doesn't `find_spec`. 24h (`_COOLDOWN_S_DEFAULT` /
`$TASK_LAYA_AUTOINSTALL_COOLDOWN_S`) is long enough that a genuinely offline (or genuinely
broken) machine isn't retried on every invocation, short enough that a machine
reconnected/fixed overnight tries again the next morning with no manual nudge required. A
GENUINELY successful attempt writes no cooldown marker at all, AND (per the site-marker note
above) a fresh process reuses it for free -- so, unlike an earlier revision of this design, a
successful dedicated-venv install stays cheap and silent on every later invocation too, not
just the first one. The marker itself is written atomically (temp file + `os.replace`,
mirroring `daemon.py`'s `save_notified`) so a concurrent `task classify` reading it mid-write
can only ever see a complete file or the prior one, never a truncated one.

FAIL-OPEN, matching `stale_notice.py`'s own NaN/non-dict-cache handling: an absent, corrupted,
or unreadable cache file all read back as "never attempted" (0.0), so a broken/hand-edited
marker can only ever cause an EXTRA attempt, never a permanent "auto-install is now wedged
off" state.

TIMEOUT: `_SUBPROCESS_TIMEOUT_S` bounds every install/venv-creation subprocess this module
makes -- a plain in-venv `pip install`, dedicated-venv creation, and the dedicated-venv `pip
install` -- so a hung/stalled step can never hang `task classify` itself. Installing a small
pure-Python wheel (or creating a venv) normally takes seconds; a few minutes is generous
headroom without risking an unbounded hang. The interpreter sanity probe and the
`sysconfig.get_path("purelib")` query normally finish in well under a second, so they get the
much shorter `_PROBE_TIMEOUT_S` (review finding: a wedged venv interpreter would otherwise
burn the full install budget on each probe before the rebuild even starts).

TEST HERMETICITY: `tests/conftest.py`'s autouse `_no_real_laya_autoinstall` fixture
monkeypatches :func:`ensure_installed` to `False` for every test EXCEPT this module's own
(`tests/test_laya_autoinstall.py`) and `tasklib/classifiers.py`'s wiring tests
(`tests/test_classifiers.py`, which mock `ensure_installed` themselves anyway) -- see that
fixture's docstring for why a real, unmocked classify test must never risk a real
subprocess/network call, a real venv creation, or a real package install into the machine
running the suite.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import logging
import math
import os
import shutil
import site
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_COOLDOWN_S_DEFAULT = 24 * 60 * 60  # 24h -- see module docstring's COOLDOWN CACHE note
_SUBPROCESS_TIMEOUT_S = 180  # 3 minutes per install subprocess -- see module docstring's TIMEOUT note
_PROBE_TIMEOUT_S = 20  # interpreter probe / sysconfig query -- see module docstring's TIMEOUT note
_MAX_DETAIL_CHARS = 200  # truncate a printed failure detail so one failure can't flood stderr

# The spec `pyproject.toml`'s `laya` extra pins -- see module docstring's VERSION PIN note.
# `tests/test_laya_autoinstall.py` asserts this matches `pyproject.toml` via `tomllib`.
_LAYA_PIP_SPEC = "laya>=0.3.7,<0.4"

# The "never raises" boundaries below (`install`, `ensure_installed`, `_activate_venv_site`,
# `_laya_importable`) catch everything by contract, so each records what it swallowed here at
# DEBUG: silent by default (no stderr beyond the one status line), but the traceback is one
# `logging.basicConfig(level=logging.DEBUG)` away instead of being discarded.
_logger = logging.getLogger(__name__)


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


def _account_home() -> Path | None:
    """The OS account's home directory from the password database, never `$HOME` (review
    finding, security): `_state_dir`'s fallback must not be derivable from the same env
    channel it refuses. Windows has no `pwd` module; there the stdlib `~` expansion
    (`%USERPROFILE%`) is the only source, so on Windows this fallback is env-derived and only
    `_is_inside_untrusted_cwd`'s containment check still applies. `None` when `pwd` exists
    but the uid has no entry (containers running an arbitrary `--user` uid): falling back to
    `~` there would read the very `$HOME` that was just refused (review finding)."""
    try:
        import pwd
    except ImportError:
        return Path(os.path.expanduser("~"))
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        return None


_EPHEMERAL_STATE_DIR: Path | None = None  # see _trusted_default_state_dir


def _trusted_default_state_dir() -> Path:
    """`_state_dir`'s fallback: `<account home>/.local/state/task-cli/laya_autoinstall`. With
    no account home (`_account_home` is `None`) it is a fresh `mkdtemp` directory kept for
    this process only: created empty and private, so nothing can be pre-planted in it; the
    cost is that such a process installs into a throwaway venv instead of a persistent one."""
    global _EPHEMERAL_STATE_DIR
    home = _account_home()
    if home is not None:
        return home / ".local" / "state" / "task-cli" / "laya_autoinstall"
    if _EPHEMERAL_STATE_DIR is None:
        try:
            _EPHEMERAL_STATE_DIR = Path(tempfile.mkdtemp(prefix="task-cli-laya-"))
        except OSError:
            # Unwritable temp dir: an uncreatable path makes every install step fail cleanly.
            return Path(os.path.abspath(os.sep)) / "nonexistent-task-cli-laya-state"
    return _EPHEMERAL_STATE_DIR


def _is_inside_untrusted_cwd(path: Path) -> bool:
    """Whether `path` (already resolved) is the current directory, nested inside it, or nested
    inside the git work tree that encloses it. `task classify` runs with cwd somewhere in the
    untrusted target repo, not necessarily at its root, so a cwd-only check would let
    `/repo/.state` through from `/repo/sub` (review finding, security). The work-tree root is
    found by walking up to the nearest `.git` entry (a directory, or a file for linked
    worktrees/submodules) -- a stat per ancestor, no `git` subprocess. Fails closed: an
    unresolvable cwd counts as untrusted."""
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        return True
    roots = [cwd]
    for ancestor in (cwd, *cwd.parents):
        if (ancestor / ".git").exists():
            roots.append(ancestor)
            break
    return any(path == root or root in path.parents for root in roots)


def _state_dir(env: dict[str, str] | None = None) -> Path:
    """The base dir for this feature's cache file and dedicated venv (`$XDG_STATE_HOME` ->
    `$HOME/.local/state`), the same base `session.py`/`read_tracker.py`/`stale_notice.py`/
    `daemon.py` all share. `env` overrides `os.environ` for hermetic tests; production
    callers always pass `None`.

    DISTRUSTS the env-derived result the same way `_resolve_trusted_uv` distrusts a resolved
    `uv` binary (review finding, security): `task classify` runs with cwd set to the
    untrusted target repo, and a poisoned/relative `$XDG_STATE_HOME` or `$HOME` (ambient
    shell env, or a malicious checkout's auto-loaded `.env` via `uv run`) could otherwise
    resolve the dedicated venv's directory to somewhere INSIDE that repo.
    `_try_activate_cached_dedicated_venv` bolts whatever lives there onto `sys.path` via a
    zero-subprocess marker fast path that runs BEFORE any cooldown/trust check -- an
    attacker-planted fake venv there would get imported outright. A RELATIVE result is
    refused outright (the XDG Base Directory spec says relative paths are invalid and must be
    ignored, and any relative path -- even one that escapes via `..` -- is anchored at that
    untrusted cwd); an absolute one inside cwd or its enclosing git work tree
    (`_is_inside_untrusted_cwd`) is refused too. Either way, fall back to
    `_trusted_default_state_dir()`, built from the password database rather than `$HOME` so
    the fallback is not steerable through the channel being refused. An accepted override is
    returned RESOLVED, so a symlink inside the repo can't be re-pointed after the check."""
    env = os.environ if env is None else env
    base = env.get("XDG_STATE_HOME") or os.path.join(env.get("HOME", ""), ".local", "state")
    candidate = Path(base) / "task-cli" / "laya_autoinstall"
    if not candidate.is_absolute():
        return _trusted_default_state_dir()
    try:
        resolved = candidate.resolve()
    except OSError:
        return _trusted_default_state_dir()
    if _is_inside_untrusted_cwd(resolved):
        return _trusted_default_state_dir()
    return resolved


def cache_path(env: dict[str, str] | None = None) -> Path:
    """Where the last FAILED attempt's timestamp is recorded (no marker on a genuinely
    successful, importable install -- see module docstring's COOLDOWN CACHE note)."""
    return _state_dir(env) / "last_failed_attempt.json"


def _dedicated_venv_dir(env: dict[str, str] | None = None) -> Path:
    """Where the dedicated, self-owned venv (module docstring's DEDICATED VENV note, case 2)
    lives -- a sibling directory to the cooldown-cache file, under the same state root, keyed
    by `sys.version_info` so a differently-versioned interpreter never reuses it (see module
    docstring for why)."""
    tag = f"{sys.version_info.major}.{sys.version_info.minor}"
    return _state_dir(env) / f"laya_venv-py{tag}"


def _venv_python_path(venv_dir: Path) -> Path:
    """The venv's own interpreter path -- the standard `venv`-module layout, which `uv venv`
    also produces, on both platforms it can run on."""
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_site_marker_path(venv_dir: Path) -> Path:
    """Where the resolved site-packages path from the last successful dedicated-venv install
    is cached (module docstring's DEDICATED VENV note) -- INSIDE the venv directory itself so
    a `--clear` rebuild of a broken venv automatically drops a stale marker along with
    everything else, never leaving it to point at a directory that no longer matches."""
    return venv_dir / ".laya_site_packages"


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
    """Record that an install attempt just failed (the install itself failed, OR it reported
    success but the package is still not importable)."""
    now = time.time() if now is None else now
    _write_last_attempt(cache_path(env), now)


def _truncate(text: str) -> str:
    text = text.strip().replace("\n", " ")
    return text if len(text) <= _MAX_DETAIL_CHARS else text[:_MAX_DETAIL_CHARS] + "…"


def _run_subprocess(
    argv: list[str], *, timeout: float, cwd: str | None = None, proc_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess | None:
    """`subprocess.run(argv, ...)`, folding a timeout / missing-interpreter / any other
    `OSError` into `None` rather than raising -- the caller gets one uniform "this attempt
    didn't complete" signal regardless of which of those actually happened. Also catches
    `UnicodeDecodeError` (a `ValueError` subclass): `text=True` decodes the subprocess's output
    with the locale codec, which can raise on non-decodable bytes on a `LC_ALL=C`/POSIX-locale
    machine -- an undecodable byte must not escape as an uncaught exception and silently skip
    the cooldown marker this function exists to let the caller record. Used for every
    subprocess call this module makes (plain install, venv creation, venv install, the
    interpreter sanity probe, and the site-packages-path query) -- see module docstring's
    TIMEOUT note. `cwd`/`proc_env` -- see `_neutral_subprocess_cwd`'s docstring for why every
    call site threads a non-default one through.
    """
    try:
        # check=False: every caller reads `returncode` itself (`_run_and_check` folds it).
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=proc_env, check=False
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def _run_and_check(
    argv: list[str], *, timeout: float, cwd: str | None = None, proc_env: dict[str, str] | None = None
) -> tuple[bool, str]:
    """Run `argv` and fold it into the one uniform `(ok, detail)` shape every install step in
    this module needs: `(True, "")` on a clean zero exit, `(False, detail)` for a
    timeout/could-not-run (`_run_subprocess` returned `None`) or a nonzero exit (the last
    non-empty stderr line, truncated -- pip and uv print the actual error last) -- a single
    place for that "did it work, and if not why" logic instead of three near-identical
    hand-written copies (plain install, dedicated-venv creation, dedicated-venv install)."""
    result = _run_subprocess(argv, timeout=timeout, cwd=cwd, proc_env=proc_env)
    if result is None:
        return False, "did not complete (timeout or could not run)"
    if result.returncode != 0:
        lines = [line for line in (result.stderr or "").splitlines() if line.strip()]
        return False, _truncate(lines[-1] if lines else f"exited {result.returncode}")
    return True, ""


def _neutral_subprocess_cwd(env: dict[str, str] | None) -> str:
    """The `cwd` for EVERY subprocess this module spawns (review finding, security): `task
    classify` runs with its own cwd set to the FOREIGN repo being classified (see
    `tasklib/classifiers.py`'s SECURITY notes) -- without an explicit override, every child
    here would inherit that untrusted directory. `-P` (see `_ensure_dedicated_venv`'s
    docstring) closes the Python-stdlib-shadowing angle of that, but `uv` ALSO walks cwd (and
    its ancestors) looking for `uv.toml` / a `pyproject.toml`'s `[tool.uv]` table, which `-P`
    does not touch at all -- a malicious repo could plant a `[tool.uv.pip] index-url`
    redirecting `uv pip install laya` to an attacker-controlled package index serving a
    malicious wheel, executed via `site.addsitedir`'s `.pth` handling once installed.
    `_state_dir` is task-cli's OWN directory, never the target repo (the same trust boundary
    this module already relies on for the cooldown cache and the dedicated venv itself) --
    created here if it doesn't exist yet, so every child always has a valid, neutral cwd to
    start from. Falls back to the system temp dir on a state-dir creation failure (matches
    `_write_last_attempt`'s own best-effort `mkdir`) rather than leaving `cwd` unset -- unless
    `$TMPDIR` resolves into the untrusted repo too (review finding), in which case the
    filesystem root is the neutral cwd."""
    state_dir = _state_dir(env)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        return str(state_dir)
    except OSError:
        pass
    tmp = tempfile.gettempdir()
    try:
        if not _is_inside_untrusted_cwd(Path(tmp).resolve()):
            return tmp
    except OSError:
        pass
    return os.path.abspath(os.sep)


# Package-index env vars a pip/uv child could otherwise inherit and use to redirect this
# module's install away from the real PyPI (review finding, security) -- see
# `_sanitized_child_env`'s docstring for the exact attack this closes.
_SCRUBBED_CHILD_ENV_VARS = (
    "PYTHONPATH",
    # Replaces the stdlib prefix outright, so `-P` alone cannot stop a planted `venv.py`
    # from running in a child (review finding).
    "PYTHONHOME",
    # PyPI / package-index URL overrides -- same attack class as PYTHONPATH but for the
    # package SOURCE rather than the import path.
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "PIP_FIND_LINKS",
    "PIP_CONFIG_FILE",
    "UV_INDEX_URL",
    "UV_EXTRA_INDEX_URL",
    "UV_INDEX",
    "UV_DEFAULT_INDEX",
    "UV_FIND_LINKS",  # uv equivalent of PIP_FIND_LINKS (--find-links / -f)
    "UV_CONFIG_FILE",  # uv's per-project/user config-file override (defense in depth alongside UV_NO_CONFIG)
    # Cache-dir overrides (review finding): an attacker can plant malicious .whl files in a
    # controlled directory and point pip/uv's cache there -- the package content served from
    # cache is trusted without re-fetching from the index.
    "PIP_CACHE_DIR",
    "UV_CACHE_DIR",
    # Insecure-host override: allows plain HTTP (no TLS) to a package-index host, enabling
    # MITM injection of malicious packages.
    "UV_INSECURE_HOST",
    # Install-DESTINATION redirects (review finding): pip would install laya outside the venv
    # and still exit 0, a false "installed" that then fails the importability re-check.
    "PIP_TARGET",
    "PIP_PREFIX",
    "PIP_ROOT",
    "PIP_USER",
)


def _sanitized_child_env(*, extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for EVERY child subprocess this module spawns (review finding,
    security): this module already gives every child a neutral `cwd`
    (`_neutral_subprocess_cwd`) because `task classify` runs with cwd set to the untrusted
    target repo -- but `_SCRUBBED_CHILD_ENV_VARS` are all SEPARATE, cwd-independent channels.
    An inherited `PYTHONPATH` could redirect a plain `python -c "import sysconfig"` (or pip's
    own internal imports) to attacker-controlled modules regardless of `-P`, which only closes
    the auto-prepended cwd entry, never explicit `PYTHONPATH` content (mirrors
    `tasklib/daemon.py`'s own `_sanitized_pythonpath_entries`/`_sanitized_child_env` rationale
    for the identical class of hole). The `PIP_*`/`UV_*` index-URL variables are the same hole
    for the package SOURCE itself: a value ambient in the parent shell, OR auto-loaded from
    the untrusted target repo's own `.env` by `uv run` (`_neutral_subprocess_cwd` narrows
    `uv`'s cwd-walking config-file discovery but does not touch already-loaded env vars),
    could redirect `pip install`/`uv pip install` to an attacker-controlled index serving a
    malicious `laya` wheel -- installed into either the ambient venv or the dedicated one,
    its `.pth` machinery then executes in-process via `site.addsitedir()` (module docstring's
    DEDICATED VENV note). Scrubbed unconditionally for every child, not just the `uv` ones.
    Deliberately NOT scrubbed: proxy and CA-bundle variables (`HTTPS_PROXY`, `SSL_CERT_FILE`,
    `REQUESTS_CA_BUNDLE`, `PIP_CERT`) -- corporate networks legitimately need them to reach
    PyPI at all. An attacker who controls both a proxy and a CA bundle in this process's env
    can still man-in-the-middle the install; that residual is accepted (the same env control
    already reaches the parent interpreter far more directly).
    `extra` layers provider-specific hardening on top (`UV_NO_CONFIG=1` for `uv` children --
    see `_ensure_dedicated_venv`'s docstring) without duplicating the scrub."""
    proc_env = dict(os.environ)
    for name in _SCRUBBED_CHILD_ENV_VARS:
        proc_env.pop(name, None)
    if extra:
        proc_env.update(extra)
    return proc_env


def _uv_proc_env() -> dict[str, str]:
    """The environment for a `uv` CHILD process specifically: `_sanitized_child_env` (every
    child gets `PYTHONPATH` scrubbed) plus `UV_NO_CONFIG=1` (review finding, defense in depth
    alongside `_neutral_subprocess_cwd`) -- `UV_NO_CONFIG` unconditionally disables `uv`'s own
    project/user config-file discovery (`uv.toml` / `pyproject.toml`'s `[tool.uv]` table), a
    documented, cwd-AND-ancestor-walking channel a neutral `cwd` alone narrows but a
    defense-in-depth belt still closes outright (e.g. against a state dir that itself sits
    under an ancestor directory carrying a stray `uv.toml`)."""
    return _sanitized_child_env(extra={"UV_NO_CONFIG": "1"})


def _resolve_trusted_uv() -> str | None:
    """Resolve `uv` the way `shutil.which` would, but DISTRUSTING the current process's cwd
    (review finding, security): `task classify` runs with cwd = the untrusted target repo.
    `shutil.which` consults an empty/relative `$PATH` entry as "the current directory" (POSIX
    shell convention) and, separately, ALWAYS checks the current directory first on Windows
    regardless of the `path` argument -- either way, a target repo that ships its own
    `uv`/`uv.exe` (or a cwd-relative `bin/` on `$PATH`) could get resolved and EXECUTED as
    THIS module's own `uv`, fully arbitrary code as the `task` process. Builds a `$PATH` with
    every empty and non-absolute entry dropped, resolves `uv` against THAT, then -- belt and
    suspenders against the Windows always-checks-cwd behavior and any absolute-but-symlinked-
    into-cwd trick -- refuses (returns `None`, which routes `install()` to the always-safe
    stdlib `venv`/`pip` fallback) if the resolved binary is itself inside the current working
    directory or its enclosing git work tree (`_is_inside_untrusted_cwd`)."""
    raw_path = os.environ.get("PATH", "")
    sanitized_entries = [entry for entry in raw_path.split(os.pathsep) if entry and os.path.isabs(entry)]
    if not sanitized_entries:
        return None
    found = shutil.which("uv", path=os.pathsep.join(sanitized_entries))
    if not found:
        return None
    try:
        resolved = Path(found).resolve()
    except OSError:
        return None
    if _is_inside_untrusted_cwd(resolved):
        return None
    return str(resolved)


def _probe_python(python_exe: Path, *, timeout: float, cwd: str) -> bool:
    """Whether `python_exe` is a runnable interpreter -- the dedicated venv's own sanity
    check (module docstring's DEDICATED VENV note): a missing/corrupt venv fails this cheaply
    (a nonexistent path just raises `OSError`, folded into `None` by `_run_subprocess`) rather
    than permanently wedging the feature off. `-P`/`cwd`/`proc_env` -- see
    `_ensure_dedicated_venv`'s docstring for why every Python child this module spawns needs
    them."""
    ok, _ = _run_and_check(
        [str(python_exe), "-P", "-c", "import sys"],
        timeout=min(timeout, _PROBE_TIMEOUT_S),
        cwd=cwd,
        proc_env=_sanitized_child_env(),
    )
    return ok


def _ensure_dedicated_venv(
    *, uv: str | None, timeout: float, env: dict[str, str] | None, cwd: str
) -> tuple[Path | None, str | None]:
    """Return `(venv_python, None)` for a working dedicated venv, reusing an existing one that
    still passes `_probe_python`, or `(None, detail)` if creating/repairing it failed. `uv` is
    resolved ONCE by the caller (`_install_into_dedicated_venv`) and threaded through here, so
    creation and install always agree on whether it's available -- resolving it independently
    in each function could let one see `uv` on `$PATH` and the other not, if `$PATH` changes
    between the two calls (review finding). `cwd` is likewise computed ONCE by the caller
    (`_neutral_subprocess_cwd`) and threaded through -- see that function's docstring for why
    every subprocess call here needs a non-default one.

    `uv venv --seed --clear` (not bare `uv venv`): `--seed` seeds pip into the new venv even
    though `uv pip install` itself doesn't need it (module docstring's DEDICATED VENV note) --
    if `uv` is present at CREATION time but later disappears from `$PATH`, an unseeded venv
    would have no pip at all, and the stdlib-pip fallback in `_install_into_dedicated_venv`
    would fail every time with no way to self-heal. `--clear` (review finding) matches the
    stdlib path below: without it, `uv venv` refuses to (re)create over an existing
    non-empty-but-not-a-venv directory (an interrupted rebuild, a partial manual delete --
    exactly the corruption `_probe_python` failing is meant to cover), which would otherwise
    leave the "self-heals by rebuilding" promise true only on the stdlib path. Both paths
    target `sys.executable`'s exact interpreter/ABI (module docstring's `ALWAYS sys.executable`
    note).

    Every Python CHILD process this module spawns (this probe, the stdlib venv-creation/
    install fallback, the sysconfig query) passes `-P`: `task classify` runs with its cwd set
    to the FOREIGN repo being classified (see `tasklib/classifiers.py`'s SECURITY notes), and
    without `-P` a bare `python -c`/`-m` prepends that untrusted cwd to `sys.path[0]` -- a
    planted `sysconfig.py`/`venv.py`/`pip/` in that repo could otherwise shadow the real
    stdlib module and run arbitrary code in the `task` process (review finding: this is
    exactly the cwd-shadowing class `LayaClassifier._load_agent` and `install_laya.sh`'s own
    `PYTHONSAFEPATH=1` already guard against elsewhere). `uv`'s own argv needs no `-P` -- it
    is a separate compiled binary, not a Python interpreter reading `sys.path` -- but it DOES
    get the neutral `cwd` and `UV_NO_CONFIG=1` (`_uv_proc_env`): `-P` only closes Python's own
    `sys.path[0]` auto-prepend, not `uv`'s OWN cwd-and-ancestor-walking config-file discovery
    (review finding, security).
    """
    venv_dir = _dedicated_venv_dir(env)
    venv_python = _venv_python_path(venv_dir)
    if _probe_python(venv_python, timeout=timeout, cwd=cwd):
        return venv_python, None

    if uv:
        create_argv = [uv, "venv", "--seed", "--clear", "--python", sys.executable, str(venv_dir)]
        create_ok, create_detail = _run_and_check(
            create_argv, timeout=timeout, cwd=cwd, proc_env=_uv_proc_env()
        )
    else:
        create_argv = [sys.executable, "-P", "-m", "venv", "--clear", str(venv_dir)]
        create_ok, create_detail = _run_and_check(
            create_argv, timeout=timeout, cwd=cwd, proc_env=_sanitized_child_env()
        )
    if not create_ok:
        return None, create_detail
    if not _probe_python(venv_python, timeout=timeout, cwd=cwd):
        return None, "venv was created but its interpreter failed a sanity check"
    return venv_python, None


def _venv_site_packages(venv_python: Path, *, timeout: float, cwd: str) -> str | None:
    """Ask the venv's OWN interpreter where its site-packages directory lives
    (`sysconfig.get_path("purelib")`) -- correct across platforms/Python versions without
    hand-computing `lib/pythonX.Y/site-packages` (module docstring's DEDICATED VENV note).
    `-P`/`cwd`/`proc_env` -- see `_ensure_dedicated_venv`'s docstring for why."""
    result = _run_subprocess(
        [str(venv_python), "-P", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        timeout=min(timeout, _PROBE_TIMEOUT_S),
        cwd=cwd,
        proc_env=_sanitized_child_env(),
    )
    if result is None or result.returncode != 0:
        return None
    # Last non-empty line: a `.pth`/`sitecustomize` that prints at startup must not turn the
    # path into multi-line garbage that then fails the containment check (review finding).
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    return lines[-1] if lines else None


def _activate_venv_site(site_packages: str) -> None:
    """Make the dedicated venv's site-packages importable in THIS already-running process --
    the same `site.addsitedir` fix the old `--user`-install era used for the identical
    "brand-new directory not on `sys.path` at interpreter startup" problem (module docstring's
    DEDICATED VENV note). Never raises: `site` is stdlib and this is pure `sys.path`/`.pth`
    bookkeeping."""
    try:
        site.addsitedir(site_packages)
    except Exception:
        _logger.debug("site.addsitedir(%r) failed", site_packages, exc_info=True)


def _is_within_dedicated_venv(site_packages: str, venv_dir: Path) -> bool:
    """Refuse to `site.addsitedir()` a path that doesn't actually live inside the dedicated
    venv THIS module created (review finding, defense in depth alongside `-P` above): a
    hijacked `sysconfig` query or a tampered `.laya_site_packages` marker resolving to an
    attacker-controlled directory must never get bolted onto `sys.path` -- `site.addsitedir`
    executes `.pth`-file `import` lines, so an arbitrary resolved path is arbitrary code
    execution in the `task` process."""
    try:
        resolved = Path(site_packages).resolve()
        venv_resolved = venv_dir.resolve()
    except OSError:
        return False
    return resolved == venv_resolved or venv_resolved in resolved.parents


def _try_activate_cached_dedicated_venv(*, env: dict[str, str] | None = None) -> bool:
    """Zero-subprocess reuse of an already-resolved dedicated venv (module docstring's
    DEDICATED VENV note): once a dedicated-venv install has succeeded once,
    `_install_into_dedicated_venv` caches the resolved site-packages path in the venv
    directory itself (`_venv_site_marker_path`). A later, fresh process can bolt it onto
    `sys.path` for free here instead of re-spawning the probe/pip/sysconfig subprocesses (and
    re-printing the install status line) on EVERY single invocation -- review finding: without
    this, that cost was paid forever, which contradicted this module's own COOLDOWN CACHE
    rationale for why a success marker matters. Never raises; a missing/unreadable/stale
    marker (or a since-deleted venv) just means "nothing to reuse yet", not a failure -- the
    normal `install()` path still runs as a fallback.
    """
    venv_dir = _dedicated_venv_dir(env)
    try:
        site_packages = _venv_site_marker_path(venv_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not site_packages or not Path(site_packages).is_dir():
        return False
    if not _is_within_dedicated_venv(site_packages, venv_dir):
        return False
    _activate_venv_site(site_packages)
    importlib.invalidate_caches()
    return _laya_importable()


@contextlib.contextmanager
def _dedicated_venv_lock(venv_dir: Path):
    """Serialize dedicated-venv create/install across processes (review finding): two
    `task classify` runs on a fresh machine (e.g. the tg inbound hook firing twice) would
    otherwise both `--clear` the same directory, one wiping the venv mid-install of the
    other, and the loser writes a 24h cooldown marker. An exclusive `flock` on a sibling
    `<venv>.lock` file (outside the venv, so `--clear` never deletes it) makes the second
    process wait; by then the venv exists, its probe passes and its install is a no-op. The
    lock is advisory and best-effort: no `fcntl` (Windows) or an unopenable lock file just
    proceeds unlocked, exactly as before."""
    try:
        import fcntl
    except ImportError:
        yield
        return
    try:
        fd = os.open(venv_dir.with_name(venv_dir.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        yield
        return
    try:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def _install_into_dedicated_venv(*, timeout: float, env: dict[str, str] | None) -> tuple[bool, str]:
    """Case 2 of `install()` (module docstring's DEDICATED VENV note): ensure the dedicated
    venv exists and works, install `_LAYA_PIP_SPEC` into IT (never the ambient interpreter),
    then bolt its site-packages onto THIS process's `sys.path` and cache the resolved path for
    later, cheap reuse (`_try_activate_cached_dedicated_venv`). Runs under
    `_dedicated_venv_lock`."""
    venv_dir = _dedicated_venv_dir(env)  # computed ONCE, reused for creation/install/marker
    cwd = _neutral_subprocess_cwd(env)  # computed ONCE (also creates the state dir)
    with _dedicated_venv_lock(venv_dir):
        return _install_into_dedicated_venv_locked(venv_dir=venv_dir, cwd=cwd, timeout=timeout, env=env)


def _install_into_dedicated_venv_locked(
    *, venv_dir: Path, cwd: str, timeout: float, env: dict[str, str] | None
) -> tuple[bool, str]:
    uv = _resolve_trusted_uv()  # resolved ONCE, threaded through -- see _ensure_dedicated_venv
    venv_python, prep_detail = _ensure_dedicated_venv(uv=uv, timeout=timeout, env=env, cwd=cwd)
    if venv_python is None:
        return False, prep_detail or "could not prepare the dedicated venv"

    install_argv, install_env = _install_plan(uv, str(venv_python))
    ok, install_detail = _run_and_check(install_argv, timeout=timeout, cwd=cwd, proc_env=install_env)
    if not ok:
        return False, install_detail

    site_packages = _venv_site_packages(venv_python, timeout=timeout, cwd=cwd)
    if site_packages is None:
        return False, "installed into the venv but could not resolve its site-packages path"
    if not _is_within_dedicated_venv(site_packages, venv_dir):
        return False, "installed, but the resolved site-packages path was outside the dedicated venv"
    _activate_venv_site(site_packages)
    try:
        marker = _venv_site_marker_path(venv_dir)
        marker.parent.mkdir(parents=True, exist_ok=True)
        tmp = marker.with_name(marker.name + ".tmp")
        tmp.write_text(site_packages, encoding="utf-8")
        os.replace(tmp, marker)
    except OSError:
        pass
    return True, "installed (venv)"


def _install_plan(uv: str | None, python: str) -> tuple[list[str], dict[str, str]]:
    """The `(argv, env)` that installs `_LAYA_PIP_SPEC` into `python`'s environment -- shared
    by both `install()` cases so the uv/pip pairing (uv gets `UV_NO_CONFIG`, pip gets `-P`,
    both get the scrubbed env) cannot drift between them."""
    if uv:
        return [uv, "pip", "install", "--python", python, _LAYA_PIP_SPEC], _uv_proc_env()
    return [python, "-P", "-m", "pip", "install", _LAYA_PIP_SPEC], _sanitized_child_env()


def install(
    *, timeout: float = _SUBPROCESS_TIMEOUT_S, env: dict[str, str] | None = None
) -> tuple[bool, str]:
    """Install `_LAYA_PIP_SPEC`, routing around PEP 668 by construction rather than by
    bypassing it (module docstring's DEDICATED VENV note): straight into `sys.executable`'s
    own environment when THIS process is already inside a virtualenv (case 1), else into a
    separate dedicated venv this module owns, bolted onto this process's `sys.path` on success
    (case 2). Never raises.

    Case 1 prefers a trusted `uv pip install --python sys.executable` over `python -m pip`:
    `uv tool install hyper-task` (the README's recommended install) builds its tool venv
    WITHOUT pip, so `-m pip` fails there on every attempt; pip is only the fallback when no
    trusted `uv` resolves.

    Returns `(success, detail)`: on success `detail` is a short human-readable note
    ("installed" / "installed (venv)"); on failure it is a truncated, single-line tail of the
    failing subprocess's stderr (or a timeout/could-not-run note).
    """
    try:
        if _in_virtualenv():
            argv, proc_env = _install_plan(_resolve_trusted_uv(), sys.executable)
            ok, detail = _run_and_check(
                argv, timeout=timeout, cwd=_neutral_subprocess_cwd(env), proc_env=proc_env
            )
            return (True, "installed") if ok else (False, detail)
        return _install_into_dedicated_venv(timeout=timeout, env=env)
    except Exception as exc:
        _logger.debug("unexpected error installing laya", exc_info=True)
        return False, _truncate(str(exc)) or "unexpected error installing laya"


def _laya_importable() -> bool:
    """`importlib.util.find_spec("laya") is not None`, never raising -- the same "is the
    package importable right now" probe `LayaClassifier.available()` uses, duplicated here
    (not imported from classifiers.py) to keep this module's only dependency on the standard
    library, matching its stdlib-only-at-import-time contract."""
    try:
        return importlib.util.find_spec("laya") is not None
    except Exception:
        _logger.debug("find_spec('laya') raised", exc_info=True)
        return False


def ensure_installed(*, env: dict[str, str] | None = None, now: float | None = None) -> bool:
    """The single entry point `LayaClassifier.available()` calls on the "package not
    importable yet" path.

    First tries the zero-subprocess dedicated-venv reuse path
    (`_try_activate_cached_dedicated_venv`, case 2 only -- see module docstring's DEDICATED
    VENV note): a previously successful dedicated-venv install is cached and can be bolted
    back onto `sys.path` for free. Failing that, skips silently (no subprocess, no output)
    when the cooldown hasn't elapsed since the last failed attempt. Otherwise prints ONE brief
    status line to stderr, attempts the install (`install()`, against `sys.executable` or its
    dedicated venv -- see that function's own docstring), invalidates import caches, then
    re-verifies importability before declaring success -- a successful `install()` does not,
    by itself, guarantee `import laya` will now work in THIS process (e.g. a genuinely
    broken/incompatible wheel, or a dedicated-venv site-packages path that failed to resolve).
    Either kind of failure (the install itself failing, or it succeeding but the package still
    not importable) records the SAME cooldown marker, so a persistently "succeeds but
    unimportable" machine doesn't re-run a no-op install (and re-print this status line) on
    every invocation.

    Never raises (mirrors `LayaClassifier.available()`'s own "never raises" contract, which
    this function is the one place that could otherwise violate via a `print()` to a closed
    stderr) -- any unexpected exception ALSO marks the cooldown before degrading to `False`,
    so a weird/unexpected failure doesn't bypass the cooldown and re-attempt on every call.

    Returns whether `laya` is now genuinely importable in THIS process.
    """
    try:
        if not _in_virtualenv() and _try_activate_cached_dedicated_venv(env=env):
            return True
        if not should_attempt(now=now, env=env):
            return False
        print("laya not installed; installing now...", file=sys.stderr)
        ok, detail = install(env=env)
        if ok:
            importlib.invalidate_caches()
            if _laya_importable():
                print(f"  laya {detail}", file=sys.stderr)
                return True
            detail = f"{detail}, but the package is still not importable"
        print(f"  laya install failed ({detail}); falling back to next provider", file=sys.stderr)
        mark_failed_attempt(now=now, env=env)
        return False
    except Exception:
        _logger.debug("ensure_installed failed unexpectedly", exc_info=True)
        mark_failed_attempt(now=now, env=env)
        return False
