"""scripts/lib/install_laya.sh — install.sh's best-effort `laya` install step (task-cli#208,
task-cli#224).

Runs the real function via `bash -c 'source ...; task_install_laya "$1" "$2"'` — a real
subprocess, mirroring `tests/test_deploy_script.py`'s own real-subprocess convention. Since
task-cli#224 the script DELEGATES straight into `tasklib.laya_autoinstall.install()` instead
of hand-duplicating a pip fallback in bash (see that module's own test suite,
`tests/test_laya_autoinstall.py`, for install()'s own fully-mocked-subprocess unit coverage);
these tests exercise the SHELL GLUE (argv threading, PYTHONPATH, always-exit-0), stubbing
`install()` itself via a tiny importable shim module rather than a fake `python3` binary that
would have to reimplement pip semantics. The function must always exit 0 (best-effort,
non-fatal), regardless of outcome.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "lib" / "install_laya.sh"


def _run(
    lib_path: Path,
    python_exe: str,
    repo_root: str = "",
    timeout: float = 30,
    *,
    bash: str = "bash",
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    # `set -euo pipefail` matches install.sh's OWN context exactly (review finding) -- a
    # harness that omits it can't catch a script that only breaks under those exact options.
    # check=False: every caller asserts on returncode/stdout itself, including nonzero exits.
    return subprocess.run(
        [bash, "-c", f'set -euo pipefail; source "{lib_path}"; task_install_laya "$1" "$2"', "--", python_exe, repo_root],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        env=env,
        check=False,
    )


def _fake_repo(tmp_path: Path, *, install_body: str) -> Path:
    """A minimal importable `tasklib` package (just enough for `from tasklib.laya_autoinstall
    import install` to work) whose `install()` is a fixed-behavior stub -- lets these tests
    drive the SHELL wrapper's own logic without touching the real `install()` (already covered
    hermetically in `tests/test_laya_autoinstall.py`) or spawning any real pip/venv/network."""
    pkg = tmp_path / "tasklib"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "laya_autoinstall.py").write_text(install_body, encoding="utf-8")
    return tmp_path


def _python_without_site(tmp_path: Path) -> str:
    """A `python_exe` for the script that runs this test interpreter with `-S` (review
    finding): the script's own `find_spec("laya")` check sees the interpreter's site-packages,
    so on a developer machine that followed the README's `uv sync --extra laya` a bare
    `sys.executable` would report "already installed" and never reach the stubbed
    `install()`. `-S` hides site-packages while `PYTHONPATH=<repo_root>` (the script's own
    mechanism) still makes the fake `tasklib`/`laya` importable."""
    wrapper = tmp_path / "bin" / "python-no-site"
    wrapper.parent.mkdir()
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return str(wrapper)


def test_already_installed_short_circuits_without_calling_install(tmp_path):
    # A `tasklib.laya_autoinstall.install` that raises if ever called -- the script's own
    # `import laya` fast path must never reach it when laya is already importable.
    repo = _fake_repo(
        tmp_path,
        install_body="def install(**kw):\n    raise AssertionError('install() must not run when laya is already importable')\n",
    )
    # Fake an importable top-level `laya` module too, alongside the fake `tasklib` package.
    (repo / "laya.py").write_text("", encoding="utf-8")
    proc = _run(LIB, _python_without_site(tmp_path), str(repo))
    assert proc.returncode == 0, proc.stderr
    assert "already installed" in proc.stdout


def test_successful_install_reports_and_exits_zero(tmp_path):
    repo = _fake_repo(
        tmp_path,
        install_body="def install(**kw):\n    return True, 'installed (venv)'\n",
    )
    proc = _run(LIB, _python_without_site(tmp_path), str(repo))
    assert proc.returncode == 0, proc.stderr
    assert "laya installed (venv)" in proc.stdout


def test_failed_install_is_non_fatal_and_exits_zero(tmp_path):
    repo = _fake_repo(
        tmp_path,
        install_body="def install(**kw):\n    return False, 'no network'\n",
    )
    proc = _run(LIB, _python_without_site(tmp_path), str(repo))
    assert proc.returncode == 0, proc.stderr  # non-fatal even on failure
    assert "laya install failed" in proc.stdout
    assert "non-fatal" in proc.stdout
    assert "no network" in proc.stdout


def test_no_break_system_packages_anywhere_in_the_script_or_its_output(tmp_path):
    repo = _fake_repo(
        tmp_path,
        install_body="def install(**kw):\n    return False, 'externally-managed-environment (simulated)'\n",
    )
    proc = _run(LIB, _python_without_site(tmp_path), str(repo))
    assert proc.returncode == 0, proc.stderr
    assert "--break-system-packages" not in proc.stdout
    assert "--break-system-packages" not in proc.stderr
    assert "--break-system-packages" not in LIB.read_text(encoding="utf-8")


def test_invoking_cwd_cannot_shadow_the_repo_tasklib(tmp_path):
    # `-c` puts the invoking cwd ahead of PYTHONPATH on sys.path; PYTHONSAFEPATH=1 must keep a
    # `tasklib` planted in that cwd from replacing the real `$repo_root/tasklib`.
    repo_parent = tmp_path / "repo_parent"
    repo_parent.mkdir()
    repo = _fake_repo(repo_parent, install_body="def install(**kw):\n    return True, 'installed (venv)'\n")
    hostile_cwd = tmp_path / "hostile_cwd"
    hostile_cwd.mkdir()
    _fake_repo(hostile_cwd, install_body="def install(**kw):\n    return True, 'SHADOWED'\n")
    proc = _run(LIB, _python_without_site(tmp_path), str(repo), cwd=hostile_cwd)
    assert proc.returncode == 0, proc.stderr
    assert "laya installed (venv)" in proc.stdout
    assert "SHADOWED" not in proc.stdout


def test_inherited_pythonhome_does_not_reach_the_python_child(tmp_path):
    # A PYTHONHOME pointing at a bogus prefix would make the child load its "stdlib" from
    # there (or fail to start at all); the script must unset it before running Python.
    repo = _fake_repo(tmp_path, install_body="def install(**kw):\n    return True, 'installed (venv)'\n")
    env = {**os.environ, "PYTHONHOME": str(tmp_path / "bogus_prefix")}
    proc = _run(LIB, _python_without_site(tmp_path), str(repo), env=env)
    assert proc.returncode == 0, proc.stderr
    assert "laya installed (venv)" in proc.stdout


def test_missing_python_interpreter_is_non_fatal(tmp_path):
    # `command -v python3` resolving to a nonexistent path must never abort the surrounding
    # install.sh (set -euo pipefail) — the function still returns 0.
    repo = _fake_repo(tmp_path, install_body="def install(**kw):\n    return True, 'installed'\n")
    proc = _run(LIB, str(tmp_path / "no-such-interpreter"), str(repo))
    assert proc.returncode == 0, proc.stderr


def test_missing_repo_root_is_non_fatal():
    # No repo root at all (empty $2, e.g. a hand-invocation without the second arg) must not
    # crash -- the script short-circuits before ever trying to import tasklib.
    proc = _run(LIB, sys.executable)
    assert proc.returncode == 0, proc.stderr


def test_survives_stock_macos_bash_under_set_u(tmp_path):
    # Regression (review finding, carried over from the pre-task-cli#224 implementation): the
    # script must never expand an unset/empty variable under `set -u` on bash < 4.4 (stock
    # macOS ships bash 3.2, no Homebrew bash). `set -euo pipefail` is EXACTLY install.sh's own
    # context. Targets `/bin/bash` explicitly (this machine's real stock interpreter) so this
    # test can't be satisfied by coincidence.

    if not Path("/bin/bash").exists():
        pytest.skip("no /bin/bash on this machine")
    repo = _fake_repo(tmp_path, install_body="def install(**kw):\n    return True, 'installed'\n")
    proc = _run(LIB, _python_without_site(tmp_path), str(repo), bash="/bin/bash")
    assert proc.returncode == 0, proc.stderr
    assert "unbound variable" not in proc.stderr
