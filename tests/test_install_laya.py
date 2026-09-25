"""scripts/lib/install_laya.sh — install.sh's best-effort `laya` install step (task-cli#208).

Runs the real function via `bash -c 'source ...; task_install_laya "$1"'` against a STUB
`python3`-shaped script placed on disk (never the real interpreter, never real pip/network) —
mirrors `tests/test_deploy_script.py`'s own real-subprocess-over-a-fake-shim convention. The
function must always exit 0 (best-effort, non-fatal), regardless of outcome.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "lib" / "install_laya.sh"


def _run(
    lib_path: Path, python_exe: str, timeout: float = 30, *, bash: str = "bash"
) -> subprocess.CompletedProcess:
    # `set -euo pipefail` matches install.sh's OWN context exactly (review finding) -- a
    # harness that omits it can't catch a script that only breaks under those exact options.
    return subprocess.run(
        [bash, "-c", f'set -euo pipefail; source "{lib_path}"; task_install_laya "$1"', "--", python_exe],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _write_stub_python(tmp_path: Path, body: str) -> Path:
    stub = tmp_path / "stub_python3.sh"
    stub.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    stub.chmod(0o755)
    return stub


def test_already_installed_reports_and_exits_zero(tmp_path):
    # `python3 -c 'import laya'` succeeds -> no pip call at all, short-circuits immediately.
    stub = _write_stub_python(
        tmp_path,
        """
if [[ "$1" == "-c" && "$2" == "import laya" ]]; then
  exit 0
fi
echo "unexpected stub invocation: $*" >&2
exit 9
""",
    )
    proc = _run(LIB, str(stub))
    assert proc.returncode == 0, proc.stderr
    assert "already installed" in proc.stdout


def test_plain_pip_install_succeeds(tmp_path):
    stub = _write_stub_python(
        tmp_path,
        """
if [[ "$1" == "-c" && "$2" == "import laya" ]]; then
  exit 1  # not yet installed
fi
if [[ "$1" == "-c" && "$2" == *"sys.prefix"* ]]; then
  exit 0  # not a virtualenv -- keep --user
fi
if [[ "$1" == "-m" && "$2" == "pip" && "$3" == "install" ]]; then
  if [[ "$*" != *"--user"* ]]; then
    echo "expected --user for a non-virtualenv interpreter" >&2
    exit 9
  fi
  if [[ "$*" == *"--break-system-packages"* ]]; then
    echo "unexpected retry — plain install should have succeeded" >&2
    exit 9
  fi
  exit 0  # plain --user install succeeds
fi
echo "unexpected stub invocation: $*" >&2
exit 9
""",
    )
    proc = _run(LIB, str(stub))
    assert proc.returncode == 0, proc.stderr
    assert "attempting: " in proc.stdout
    assert "laya installed" in proc.stdout
    assert "retrying" not in proc.stdout


def test_plain_install_drops_user_flag_inside_a_virtualenv(tmp_path):
    # `pip install --user` is refused outright inside an activated virtualenv -- --user must
    # never even be attempted there (see tasklib/laya_autoinstall.py's identical Python-side
    # behavior and its module docstring's VIRTUALENV note).
    stub = _write_stub_python(
        tmp_path,
        """
if [[ "$1" == "-c" && "$2" == "import laya" ]]; then
  exit 1
fi
if [[ "$1" == "-c" && "$2" == *"sys.prefix"* ]]; then
  exit 1  # IS a virtualenv
fi
if [[ "$1" == "-m" && "$2" == "pip" && "$3" == "install" ]]; then
  if [[ "$*" == *"--user"* ]]; then
    echo "must never pass --user inside a virtualenv" >&2
    exit 9
  fi
  exit 0
fi
echo "unexpected stub invocation: $*" >&2
exit 9
""",
    )
    proc = _run(LIB, str(stub))
    assert proc.returncode == 0, proc.stderr
    assert "laya installed" in proc.stdout


def test_externally_managed_environment_retries_and_succeeds(tmp_path):
    stub = _write_stub_python(
        tmp_path,
        """
if [[ "$1" == "-c" && "$2" == "import laya" ]]; then
  exit 1
fi
if [[ "$1" == "-c" && "$2" == *"sys.prefix"* ]]; then
  exit 0
fi
if [[ "$1" == "-m" && "$2" == "pip" && "$3" == "install" ]]; then
  if [[ "$*" == *"--break-system-packages"* ]]; then
    exit 0
  fi
  echo "error: externally-managed-environment" >&2
  echo "hint: use --break-system-packages" >&2
  exit 1
fi
exit 9
""",
    )
    proc = _run(LIB, str(stub))
    assert proc.returncode == 0, proc.stderr
    assert "retrying with --break-system-packages" in proc.stdout
    assert "laya installed" in proc.stdout


def test_externally_managed_environment_retry_also_fails_is_non_fatal(tmp_path):
    stub = _write_stub_python(
        tmp_path,
        """
if [[ "$1" == "-c" && "$2" == "import laya" ]]; then
  exit 1
fi
if [[ "$1" == "-c" && "$2" == *"sys.prefix"* ]]; then
  exit 0
fi
if [[ "$1" == "-m" && "$2" == "pip" && "$3" == "install" ]]; then
  if [[ "$*" == *"--break-system-packages"* ]]; then
    exit 1  # still fails even with the retry flag
  fi
  echo "error: externally-managed-environment" >&2
  exit 1
fi
exit 9
""",
    )
    proc = _run(LIB, str(stub))
    assert proc.returncode == 0, proc.stderr  # non-fatal even after both attempts fail
    assert "retrying with --break-system-packages" in proc.stdout
    assert "install failed" in proc.stdout
    assert "non-fatal" in proc.stdout


def test_unrelated_failure_never_retries_with_break_system_packages(tmp_path):
    # An unrelated failure (no network) must not gain a --break-system-packages retry.
    stub = _write_stub_python(
        tmp_path,
        """
if [[ "$1" == "-c" && "$2" == "import laya" ]]; then
  exit 1
fi
if [[ "$1" == "-c" && "$2" == *"sys.prefix"* ]]; then
  exit 0
fi
if [[ "$1" == "-m" && "$2" == "pip" && "$3" == "install" ]]; then
  if [[ "$*" == *"--break-system-packages"* ]]; then
    echo "must never be called for an unrelated failure" >&2
    exit 9
  fi
  echo "Could not find a version that satisfies the requirement" >&2
  exit 1
fi
exit 9
""",
    )
    proc = _run(LIB, str(stub))
    assert proc.returncode == 0, proc.stderr
    assert "retrying" not in proc.stdout
    assert "install failed" in proc.stdout
    assert "non-fatal" in proc.stdout


def test_missing_python_interpreter_is_non_fatal(tmp_path):
    # `command -v python3` resolving to a nonexistent path must never abort the surrounding
    # install.sh (set -euo pipefail) — the function still returns 0.
    proc = _run(LIB, str(tmp_path / "no-such-interpreter"))
    assert proc.returncode == 0, proc.stderr


def test_venv_branch_survives_stock_macos_bash_under_set_u(tmp_path):
    # Regression (review finding): a bash-ARRAY-based `--user`-flag implementation crashes
    # with "unbound variable" under `set -u` on bash < 4.4 (stock macOS ships bash 3.2, no
    # Homebrew bash) the moment it expands a declared-but-EMPTY array in the virtualenv
    # branch -- `set -euo pipefail` is EXACTLY install.sh's own context. Targets `/bin/bash`
    # explicitly (this machine's real stock interpreter, confirmed 3.2, not whatever newer
    # bash happens to be first on `$PATH`) so this test can't be satisfied by coincidence.
    if not Path("/bin/bash").exists():
        pytest.skip("no /bin/bash on this machine")
    stub = _write_stub_python(
        tmp_path,
        """
if [[ "$1" == "-c" && "$2" == "import laya" ]]; then
  exit 1
fi
if [[ "$1" == "-c" && "$2" == *"sys.prefix"* ]]; then
  exit 1  # IS a virtualenv -- the empty-array-expansion trap
fi
if [[ "$1" == "-m" && "$2" == "pip" && "$3" == "install" ]]; then
  exit 0
fi
exit 9
""",
    )
    proc = _run(LIB, str(stub), bash="/bin/bash")
    assert proc.returncode == 0, proc.stderr
    assert "unbound variable" not in proc.stderr
    assert "laya installed" in proc.stdout
