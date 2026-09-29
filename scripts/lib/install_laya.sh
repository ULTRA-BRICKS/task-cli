#!/usr/bin/env bash
# scripts/lib/install_laya.sh — best-effort install of the optional `laya` PyPI package
# (task-cli#208's default `task classify` provider — see tasklib/classifiers.py /
# tasklib/laya_autoinstall.py) against a given python interpreter. Sourced by install.sh;
# also directly runnable for manual testing (`scripts/lib/install_laya.sh [python_exe] [repo_root]`).
#
# Factored out of install.sh's own top-to-bottom body (rather than inlined) so it can be
# exercised in isolation by `tests/test_install_laya.py` via subprocess, without needing to
# drive install.sh's full clone/symlink/install-skill flow just to reach this one step —
# mirrors how `tasklib/install.py` holds the real logic behind `task install-skill` while
# install.sh merely invokes it.
#
# NEVER FATAL: `task_install_laya` always returns 0 — every branch below either succeeds or
# prints a short status line and falls through. install.sh's own `set -e` must never abort
# the surrounding install because this best-effort step failed (offline machine, a PEP 668
# environment with no working fix, pip missing entirely, ...).
#
# task-cli#224: this used to hand-duplicate `tasklib/laya_autoinstall.py`'s own pip-retry
# logic (an escalating pip flag appended on a PEP 668 "externally-managed-environment"
# failure) as a second, maintenance-risk bash copy — that flag is explicitly banned for any
# auto-install/bootstrap path in this project (project directive, task-cli#224), and that ban
# is also why this script no longer tries to reimplement any pip-flag fallback in bash at
# all. Instead it DELEGATES straight into `tasklib.laya_autoinstall.install()` — the SAME
# Python function `task classify`'s own runtime auto-install calls (`ensure_installed`) — via
# `PYTHONPATH=<repo root>`, so there is exactly one implementation of the "how do we get
# `laya` installed without touching a PEP-668-managed system Python" algorithm (a dedicated,
# self-owned venv; see that module's own docstring for the full design and rationale), never
# two copies that can drift. `repo_root` is `install.sh`'s own `$SRC` (the checked-out/cloned
# task-cli source tree), threaded through as the second argument so `tasklib` is importable
# without a `pip install -e .` step.
task_install_laya() {
  local python_exe="${1:-python3}"
  local repo_root="${2:-}"

  if [[ -z "$repo_root" ]]; then
    echo "task: laya install skipped (best-effort, non-fatal) — no repo root given" >&2
    return 0
  fi

  local py_code
  py_code='
import importlib.util
import sys

# find_spec, not `import laya`: a PRESENT-but-broken install must not be EXECUTED here (it
# could raise mid-import) -- matches tasklib/classifiers.py, LayaClassifier.available(),
# which uses the same check for the identical reason.
if importlib.util.find_spec("laya") is not None:
    print("laya already installed")
    sys.exit(0)

from tasklib.laya_autoinstall import install

ok, detail = install()
print(f"laya {detail}" if ok else f"laya install failed (best-effort, non-fatal) — {detail}")
sys.exit(0 if ok else 1)
'

  echo "task: checking for laya (optional, task classify default provider)"
  # PYTHONSAFEPATH keeps `-c`'s implicit cwd entry out of sys.path (Python 3.11+, matches
  # pyproject.toml's requires-python floor): otherwise an arbitrary invoking cwd could shadow
  # the intended `$repo_root/tasklib` with an unrelated same-named module — the identical
  # cwd-shadowing posture `tasklib/classifiers.py`'s `LayaClassifier` already takes for its
  # own model-id lookup (see AGENTS.md's laya section). `|| true`: the command substitution's
  # own exit code (success/failure of the install) must never trip `set -e` -- this function
  # always returns 0 itself, on either outcome.
  local output
  # `env -u PYTHONHOME`: an inherited PYTHONHOME replaces the stdlib prefix, a channel
  # PYTHONSAFEPATH does not cover (same scrub as tasklib.laya_autoinstall's child env).
  output="$(env -u PYTHONHOME PYTHONSAFEPATH=1 PYTHONPATH="$repo_root" "$python_exe" -c "$py_code" 2>&1)" || true
  echo "task: $output"
  return 0
}

# Allow direct invocation for manual testing/debugging, but not when sourced by install.sh.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  task_install_laya "${1:-python3}" "${2:-}"
fi
