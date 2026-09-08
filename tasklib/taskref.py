"""Repo-aware task-code shape validation.

What: fail-closed parser for a candidate ticket code against the repo's tracker
shape (github-issues vs linear team prefix). Shape-only: no network, no ``gh``.
Existence 404 stays the backend's job.

Accessed via: GitHubIssuesBackend._issue_number / LinearBackend._issue_by_identifier
before any HTTP, and ``from tasklib.taskref import validate`` for other tools.

Assumptions: a repo's committed task.yaml / rig.yaml ``task:`` block (loaded with
include_global=False) decides the shape; zero-config default is github-issues.
validate() never raises.

Past bugs: invented codes (TEST-1, CODE, RIG-CLI-298-VERSION-BUMP, HYP-123,
ambiguous-create-dedup) were interpolated into GET /repos/.../issues/<slug>
(review-cli run-stats, agent-tools#607). github_issues._issue_number only did
ticket_id.removeprefix("#").strip().
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .logging import redact


class TaskRefError(ValueError):
    """Raised by github_issue_number / linear_identifier on a malformed code."""


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    code: str
    canonical: str = ""
    backend: str = "github-issues"
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


_GITHUB_CROSS = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([0-9]+)$")
_DIGITS = re.compile(r"^[0-9]+$")
_LINEAR = re.compile(r"^([A-Za-z][A-Za-z0-9]*)-([0-9]+)$")

_FIX = "Run `task find` to look up an existing ticket or `task new` to create one."


def _shown(code: str) -> str:
    """User-facing form of a rejected code — token-shaped values are redacted."""
    return redact(str(code))


def _fail(code: str, backend: str, what: str, why: str) -> ValidationResult:
    return ValidationResult(
        ok=False,
        code=code,
        canonical="",
        backend=backend,
        reason=f"{what} {why} {_FIX}",
    )


def _ok(code: str, canonical: str, backend: str) -> ValidationResult:
    return ValidationResult(ok=True, code=code, canonical=canonical, backend=backend)


def github_issue_number(code: str) -> str:
    """Return the same-repo issue digits, or raise TaskRefError.

    Accepts ``N`` or ``#N``. Does not accept ``owner/repo#N`` — that form is
    library-only; the GitHub backend interpolates into ``/issues/{num}``.
    """
    text = str(code).strip()
    # removeprefix, NOT lstrip("#") — lstrip strips ALL leading '#' (e.g. "##1" -> "1"),
    # mangling an id; removeprefix drops exactly one leading marker.
    # Do NOT strip again after removing '#': "# 1" / "#\t1" must stay invalid so they
    # cannot collapse onto issue 1 (codex P1 on task-cli#173).
    stripped = text.removeprefix("#")
    if _DIGITS.fullmatch(stripped):
        return stripped
    raise TaskRefError(
        f"{_shown(code)!r} is not a GitHub issue number. "
        "Same-repo codes are #N or N (digits after stripping exactly one leading #). "
        f"{_FIX}"
    )


def linear_identifier(code: str, team: str) -> str:
    """Return TEAM-N for ``team``, or raise TaskRefError."""
    team = str(team).strip()
    text = str(code).strip()
    if not team:
        raise TaskRefError(
            f"{_shown(code)!r} cannot be validated as a Linear identifier because no team is configured. "
            f"{_FIX}"
        )
    match = _LINEAR.fullmatch(text)
    if match is None or match.group(1).upper() != team.upper():
        raise TaskRefError(
            f"{_shown(code)!r} is not a Linear identifier for team {team.upper()}. "
            f"Codes must look like {team.upper()}-<n>. {_FIX}"
        )
    return f"{team.upper()}-{match.group(2)}"


def _github_canonical(code: str) -> str | None:
    text = str(code).strip()
    cross = _GITHUB_CROSS.fullmatch(text)
    if cross is not None:
        return f"{cross.group(1)}/{cross.group(2)}#{cross.group(3)}"
    try:
        return github_issue_number(text)
    except TaskRefError:
        return None


def validate(code, repo_root=None, *, backend=None, linear_team="", github_repo=""):
    """Shape-check ``code`` against the repo tracker. Never raises."""
    text = "" if code is None else str(code).strip()
    resolved_backend = backend
    team = "" if linear_team is None else str(linear_team)

    if repo_root is not None and backend is None:
        from .config import ConfigError, load

        try:
            cfg = load(Path(repo_root), include_global=False)
        except ConfigError as exc:
            return _fail(
                text,
                "unknown",
                f"Could not load task config for {repo_root}.",
                str(exc).rstrip(".") + ".",
            )
        resolved_backend = cfg.backend
        if not team.strip():
            team = str(cfg.section("linear").get("team", ""))

    if not resolved_backend:
        resolved_backend = "github-issues"

    # github_repo is accepted for API parity; this library is shape-only.
    _ = github_repo

    if resolved_backend == "github-issues":
        canonical = _github_canonical(text)
        if canonical is None:
            return _fail(
                text,
                resolved_backend,
                f"{_shown(text)!r} is not a GitHub issue number.",
                "This repo uses github-issues, which only accepts #N, N, or owner/repo#N.",
            )
        return _ok(text, canonical, resolved_backend)

    if resolved_backend == "linear":
        if not team.strip():
            return _fail(
                text,
                resolved_backend,
                f"{_shown(text)!r} cannot be validated as a Linear identifier.",
                "This repo uses linear but no team is configured "
                "(set linear.team in task.yaml or the rig.yaml task: block).",
            )
        try:
            canonical = linear_identifier(text, team)
        except TaskRefError:
            return _fail(
                text,
                resolved_backend,
                f"{_shown(text)!r} is not a Linear identifier for team {team.strip().upper()}.",
                f"This repo uses linear; codes must look like {team.strip().upper()}-<n>.",
            )
        return _ok(text, canonical, resolved_backend)

    return _fail(
        text,
        str(resolved_backend),
        f"{_shown(text)!r} cannot be validated.",
        f"Unknown backend {resolved_backend!r}; expected github-issues or linear.",
    )
