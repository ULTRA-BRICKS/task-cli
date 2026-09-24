#!/usr/bin/env bash
# scripts/lib/install_laya.sh — best-effort install of the optional `laya` PyPI package
# (task-cli#208's default `task classify` provider — see tasklib/classifiers.py /
# tasklib/laya_autoinstall.py) against a given python interpreter. Sourced by install.sh;
# also directly runnable for manual testing (`scripts/lib/install_laya.sh [python_exe]`).
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
# The two-step pip fallback here is the SAME one `tasklib/laya_autoinstall.py` uses for the
# task-classify-time auto-install (see that module's docstring for the full PEP 668 /
# virtualenv rationale): a plain `pip install --user <spec>` first, and `--break-system-
# packages` is appended ONLY on a retry triggered by that exact first attempt failing with
# "externally-managed-environment" in its output — never as a blanket first attempt, and
# never retried for an unrelated failure (no network, disk full) that would just fail the
# identical way again. `--user` confines the install to the invoking user's own site-
# packages, never a Homebrew-managed or system file, which is what makes auto-appending
# `--break-system-packages` safe here without a human in the loop — EXCEPT inside an
# activated virtualenv, where `--user` is refused outright by pip ("User site-packages are
# not visible in this virtualenv", live-verified) and is therefore dropped entirely: a
# venv's own site-packages dir is already an isolated, user-writable location, and PEP 668's
# externally-managed-environment guard is a system-Python concept that never applies there.
#
# `_LAYA_SPEC` is the SAME pinned spec `pyproject.toml`'s `laya` extra and `tasklib/
# laya_autoinstall.py`'s `_LAYA_PIP_SPEC` install — an unpinned install could silently pull a
# breaking `laya` release that `LayaClassifier._map_result`'s hand-parsed envelope can no
# longer read (see `pyproject.toml`'s own comment on why the extra is upper-bounded).
_LAYA_SPEC="laya>=0.3.7,<0.4"

task_install_laya() {
  local python_exe="${1:-python3}"

  if "$python_exe" -c 'import laya' 2>/dev/null; then
    echo "task: laya already installed"
    return 0
  fi

  # A plain string, never a bash ARRAY: install.sh runs under `set -euo pipefail`, and a
  # stock macOS `/bin/bash` (3.2, pre-4.4) treats expanding a declared-but-EMPTY array
  # (`${arr[@]}`) under `set -u` as a fatal "unbound variable" error, which would abort the
  # whole install (review finding, live-verified failure class) -- `--user` has no spaces/
  # globs, so a plain string is safe to leave unquoted below (intentional word-splitting: an
  # empty value must vanish from the argv entirely, not become an empty "" argument).
  local user_flag="--user"
  if ! "$python_exe" -c 'import sys; sys.exit(0 if sys.prefix == sys.base_prefix else 1)' 2>/dev/null; then
    user_flag=""  # a virtualenv -- --user is refused outright there
  fi

  echo "task: laya not found, attempting: $python_exe -m pip install $user_flag $_LAYA_SPEC"
  local install_log
  if install_log="$("$python_exe" -m pip install $user_flag "$_LAYA_SPEC" 2>&1)"; then
    echo "task: laya installed"
    return 0
  fi

  if [[ "$install_log" == *"externally-managed-environment"* ]]; then
    echo "task: retrying with --break-system-packages (PEP 668 externally-managed environment)"
    local retry_log
    if retry_log="$("$python_exe" -m pip install $user_flag --break-system-packages "$_LAYA_SPEC" 2>&1)"; then
      echo "task: laya installed"
      return 0
    fi
    echo "task: laya install failed (best-effort, non-fatal) — ${retry_log##*$'\n'}"
    return 0
  fi

  echo "task: laya install failed (best-effort, non-fatal, e.g. offline) — 'task classify' installs it automatically on first use instead"
  return 0
}

# Allow direct invocation for manual testing/debugging, but not when sourced by install.sh.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  task_install_laya "${1:-python3}"
fi
