"""Product-module taxonomy: a small, per-repo, NAMED list of durable product/code subsystems
(e.g. ``classify``, ``backends``, ``links``) that a ticket gets tagged into via an ordinary
``module:<name>`` label — the same mechanism ``sp:<n>``/``priority:<Pn>`` (task-cli#203) use,
so it needs zero backend-adapter changes.

Config shape (``task.yaml``'s top-level ``modules:`` key, cascaded like every other section —
see ``tasklib/config.py``)::

    modules:
      - name: classify
        description: message -> ticket classification (fallback chain, SP/priority axes)

FAIL-SOFT (empty, never-required :class:`ModuleTaxonomy`) when the key is absent; FAIL-CLOSED
(``ConfigError``) when present but malformed — :func:`validate_modules_config` is the single
source of truth for the shape, called both from ``config.validate()`` (lazy import, so the
load-time dependency only runs this module -> config, never the reverse) and from
:func:`load_taxonomy` itself.

``task.yaml`` has no config-WRITING precedent anywhere in this repo — it is committed and
human-edited, so ``task module add`` prints the YAML snippet to add by hand instead.

:func:`infer_module` is a cheap, v1, in-process keyword-overlap HEURISTIC (no model call),
used only when a repo opts in via ``classify.infer_module: true`` (default ``False``). It is
NOT a guarantee: an empty taxonomy, no boundary-matched keyword overlap, or a scoring tie all
return ``None`` (no label applied) rather than guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import ConfigError

LABEL_PREFIX = "module:"

# A module name is stamped verbatim into a `module:<name>` label (and a hand-edited YAML
# `name:` value) — lowercase letters/digits/hyphens only, starting with a letter/digit. Mirrors
# the shape discipline config.py's trusted_attachment_hosts DNS-label check applies to ITS list.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\Z")


@dataclass(frozen=True)
class ProductModule:
    """One taxonomy entry: a named subsystem/area a ticket can be assigned into."""

    name: str
    description: str = ""


@dataclass(frozen=True)
class ModuleTaxonomy:
    """The repo's configured module list — possibly empty (see :attr:`configured`)."""

    modules: tuple[ProductModule, ...] = ()

    @property
    def configured(self) -> bool:
        """Whether this repo has adopted a taxonomy at all (``modules:`` present + non-empty)."""
        return bool(self.modules)

    def names(self) -> tuple[str, ...]:
        return tuple(m.name for m in self.modules)

    def get(self, name: str) -> ProductModule | None:
        for m in self.modules:
            if m.name == name:
                return m
        return None


def validate_modules_config(raw: object) -> None:
    """Fail-closed shape check for a raw ``data["modules"]`` value.

    The single source of truth for what a valid taxonomy entry looks like — called from
    ``config.validate()`` (lazy import there avoids a load-time import cycle: this module
    imports ``config.ConfigError`` at its own top, so the dependency only runs one way) and
    from :func:`load_taxonomy` itself.
    """
    if not isinstance(raw, list):
        raise ConfigError(
            f"'modules' must be a list of {{name, description}} entries, got "
            f"{type(raw).__name__}"
        )
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ConfigError(
                f"each 'modules' entry must be a mapping with a string 'name', got {entry!r}"
            )
        name = entry["name"].strip()
        if not name or not NAME_RE.match(name):
            raise ConfigError(
                f"module name {entry['name']!r} must be lowercase letters/digits/hyphens "
                "only, starting with a letter or digit (it is stamped verbatim into a "
                "'module:<name>' label)"
            )
        if name in seen:
            raise ConfigError(f"duplicate module name {name!r} in 'modules'")
        seen.add(name)
        description = entry.get("description", "")
        if not isinstance(description, str):
            raise ConfigError(
                f"module {name!r}: 'description' must be a string, got {description!r}"
            )


def load_taxonomy(cfg) -> ModuleTaxonomy:
    """Load the cascaded ``modules:`` list off ``cfg`` (a ``config.LoadedConfig``).

    Fail-soft to an EMPTY taxonomy when the key is absent entirely — never require one; see
    the module docstring. Fail-closed (``ConfigError``) on a malformed present key.
    """
    raw = cfg.data.get("modules")
    if raw is None:
        return ModuleTaxonomy()
    validate_modules_config(raw)
    return ModuleTaxonomy(
        modules=tuple(
            ProductModule(
                name=str(entry["name"]).strip(),
                description=str(entry.get("description", "")).strip(),
            )
            for entry in raw
        )
    )


def label_for(name: str) -> str:
    """The ``module:<name>`` label ``task module assign``/classify-time inference stamps."""
    return f"{LABEL_PREFIX}{name}"


def assigned_module_names(labels: list[str]) -> set[str]:
    """Every module name already carried as a ``module:<name>`` label on a ticket."""
    return {label[len(LABEL_PREFIX):] for label in labels if label.startswith(LABEL_PREFIX)}


def _term_count(haystack: str, term: str) -> int:
    """Boundary-aware occurrence count of ``term`` in ``haystack`` — NOT a naive
    ``str.count`` substring match, so a module named ``web`` doesn't score on "website" and a
    description word ``link`` doesn't score on "hyperlink". ``term`` may itself contain a
    hyphen (e.g. a module named ``task-cli``); the boundary check only excludes adjacent
    alnum characters, so the hyphen stays part of the literal match."""
    pattern = re.compile(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])")
    return len(pattern.findall(haystack))


def infer_module(taxonomy: ModuleTaxonomy, text: str) -> str | None:
    """Cheap v1 keyword-overlap heuristic — NOT a guarantee (see module docstring).

    Scores each configured module by counting boundary-aware occurrences, in ``text``, of its
    own name plus its description's "significant" words (length > 3, to skip stopword-ish
    noise like "the"/"and"/"for"). Returns the single highest-scoring module's name, or
    ``None`` on no signal, an empty taxonomy/text, or a scoring TIE — an ambiguous signal is
    worse than no label at all.
    """
    if not taxonomy.configured or not text:
        return None
    haystack = text.lower()
    scores: dict[str, int] = {}
    for m in taxonomy.modules:
        score = _term_count(haystack, m.name.lower())
        words = {w for w in re.findall(r"[a-z0-9]+", m.description.lower()) if len(w) > 3}
        score += sum(_term_count(haystack, w) for w in words)
        if score:
            scores[m.name] = score
    if not scores:
        return None
    best = max(scores.values())
    winners = [name for name, s in scores.items() if s == best]
    return winners[0] if len(winners) == 1 else None
