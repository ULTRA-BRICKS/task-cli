"""Pure edits of a ticket's acceptance list for ``task change`` (task-cli#293).

``--acceptance`` only ever appends. ``--acceptance-replace N=TEXT``, ``--acceptance-remove N``
and ``--acceptance-clear`` are the other half: they let a wrong or superseded criterion leave a
ticket without anyone hand-editing the issue body behind the gates.

No I/O, no backend: :func:`parse_edit` turns the raw flags into an :class:`AcceptanceEdit`,
:func:`apply_edit` applies it to the current criteria. Every ``N`` is a 1-based position in the
list AS IT STANDS BEFORE the edit (the number ``task read``/``task check`` show), so
``--acceptance-remove 1 --acceptance-remove 3`` never shifts under the caller's feet. A
criterion no flag names is returned as the very same object — its checked box, proof and force
reason are never touched.

Errors are :class:`AcceptanceEditError` whose message is the 3-part what / why / fix shape the
CLI prints after ``error:``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .model import Criterion

CLEAR_FLAG = "--acceptance-clear"
REPLACE_FLAG = "--acceptance-replace"
REMOVE_FLAG = "--acceptance-remove"

_INTEGER_RE = re.compile(r"[+-]?[0-9]+", re.ASCII)


class AcceptanceEditError(ValueError):
    """An acceptance edit that cannot mean what it says (message: what / why / fix)."""


def _refuse(what: str, why: str, fix: str) -> AcceptanceEditError:
    return AcceptanceEditError(f"{what}\n  why: {why}\n  fix: {fix}")


@dataclass(frozen=True)
class AcceptanceEdit:
    """The parsed ``--acceptance-replace`` / ``--acceptance-remove`` / ``--acceptance-clear``."""

    replace: dict[int, str] = field(default_factory=dict)
    remove: frozenset[int] = frozenset()
    clear: bool = False

    @property
    def active(self) -> bool:
        return bool(self.replace or self.remove or self.clear)


@dataclass(frozen=True)
class CheckedCriterionEdit:
    """A criterion the edit rewrites or drops that is ALREADY checked — what ``--acceptance-force`` guards."""

    index: int
    action: str  # "replaced" | "removed" | "cleared"
    criterion: Criterion

    @property
    def evidence(self) -> str:
        if self.criterion.proof:
            return f"proof: {self.criterion.proof}"
        if self.criterion.force_reason:
            return f"forced: {self.criterion.force_reason}"
        return "no proof recorded"


_PLACEHOLDER = "(none specified)"  # what the body renderer writes for an empty list: parsed back as NO criterion


def _check_text(flag: str, text: str, fix: str) -> None:
    """A criterion is one non-blank checklist line the body parser reads back as exactly one criterion."""
    if not text.strip():
        raise _refuse(
            f"{flag} text is empty",
            "a criterion with no text can be neither read nor proven, and would pad the minimum count",
            fix,
        )
    if len(text.splitlines()) > 1:  # the predicate the body parser splits lines with (U+2028 included)
        raise _refuse(
            f"{flag} text has a line break",
            "a criterion is one checklist line; a second line could pose as another (checked) criterion",
            fix,
        )
    if text.strip().lower() == _PLACEHOLDER:
        raise _refuse(
            f"{flag} text is the empty-list placeholder {_PLACEHOLDER!r}",
            "it is read back as no criterion at all, so the criterion would vanish after saving",
            "write the criterion's actual wording",
        )


def _parse_index(flag: str, raw: str) -> int:
    text = raw.strip()
    if _INTEGER_RE.fullmatch(text) is None or len(text) > 9:
        raise _refuse(
            f"{flag} needs a criterion number, got {text!r}",
            "criteria are addressed by their 1-based position, the number `task read` shows",
            f"pass a whole number, e.g. {flag} 2" + ("=TEXT" if flag == REPLACE_FLAG else ""),
        )
    return int(text)


def parse_edit(replace: list[str], remove: list[str], clear: bool) -> AcceptanceEdit:
    """Parse the raw flag values. Refuses malformed values and contradicting flags; range
    against the actual list is :func:`apply_edit`'s job (it needs the list)."""
    replacements: dict[int, str] = {}
    for raw in replace:
        number, sep, text = raw.partition("=")
        if not sep:
            raise _refuse(
                f"{REPLACE_FLAG} needs N=TEXT, got {raw!r}",
                "the number says which criterion to rewrite and the text is what replaces it",
                f'pass e.g. {REPLACE_FLAG} "2=the new wording of criterion 2"',
            )
        index = _parse_index(REPLACE_FLAG, number)
        if not text.strip():
            raise _refuse(
                f"{REPLACE_FLAG} {index}= has an empty replacement text",
                "a criterion with no text can be neither read nor proven",
                f"give the new wording after the =, or use {REMOVE_FLAG} {index} to drop it",
            )
        if index in replacements:
            raise _refuse(
                f"{REPLACE_FLAG} names criterion {index} twice",
                "two replacements for one criterion leave no way to tell which one wins",
                f"pass {REPLACE_FLAG} {index}=TEXT once",
            )
        _check_text(f"{REPLACE_FLAG} {index}=", text, "write the criterion on one line")
        replacements[index] = text.strip()
    removals = frozenset(_parse_index(REMOVE_FLAG, raw) for raw in remove)
    both = sorted(removals & replacements.keys())
    if both:
        listed = ", ".join(str(n) for n in both)
        raise _refuse(
            f"{REMOVE_FLAG} and {REPLACE_FLAG} both name criterion {listed}",
            "a criterion cannot be dropped and rewritten in the same edit",
            f"pass only one of them for criterion {listed}",
        )
    if clear and (replacements or removals):
        raise _refuse(
            f"{CLEAR_FLAG} cannot be combined with {REPLACE_FLAG} or {REMOVE_FLAG}",
            f"{CLEAR_FLAG} already drops every criterion, so naming single ones contradicts it",
            f"drop {CLEAR_FLAG}, or drop the per-criterion flags and pass the new list with --acceptance",
        )
    return AcceptanceEdit(replace=replacements, remove=removals, clear=clear)


def _check_range(edit: AcceptanceEdit, count: int) -> None:
    named = sorted(edit.replace.keys() | edit.remove)
    if named and count == 0:
        raise _refuse(
            "the ticket has no acceptance criteria, so there is no criterion to replace or remove",
            f"{REPLACE_FLAG}/{REMOVE_FLAG} address existing criteria by position",
            'add criteria first with --acceptance "<criterion>" (repeatable)',
        )
    outside = [n for n in named if not 1 <= n <= count]
    if outside:
        listed = ", ".join(str(n) for n in outside)
        raise _refuse(
            f"criterion {listed} does not exist",
            f"the ticket has {count} acceptance criteria, numbered 1..{count}",
            f"pick a number in 1..{count} (`task read <id>` shows them)",
        )


def checked_edits(current: list[Criterion], edit: AcceptanceEdit) -> list[CheckedCriterionEdit]:
    """The already-checked criteria this edit would rewrite or drop, in list order."""
    touched: list[CheckedCriterionEdit] = []
    for index, crit in enumerate(current, start=1):
        if not crit.checked:
            continue
        if edit.clear:
            touched.append(CheckedCriterionEdit(index, "cleared", crit))
        elif index in edit.replace:
            touched.append(CheckedCriterionEdit(index, "replaced", crit))
        elif index in edit.remove:
            touched.append(CheckedCriterionEdit(index, "removed", crit))
    return touched


def apply_edit(
    current: list[Criterion], edit: AcceptanceEdit, additions: list[str], minimum: int
) -> list[Criterion]:
    """The criteria after ``edit`` then ``additions`` (``--acceptance``, de-duplicated against
    what remains). ``minimum`` is the smallest allowed result (0 when the gate is off or waived).
    Raises :class:`AcceptanceEditError` for a number outside the list or a result below
    ``minimum``; never mutates ``current`` or its criteria."""
    _check_range(edit, len(current))
    kept: list[Criterion] = [] if edit.clear else list(current)
    result: list[Criterion] = []
    for index, crit in enumerate(kept, start=1):
        if index in edit.remove:
            continue
        result.append(Criterion(text=edit.replace[index]) if index in edit.replace else crit)
    rewritten = [text for index, text in edit.replace.items() if index <= len(kept) and index not in edit.remove]
    texts = [crit.text for crit in result]
    for text in rewritten:  # only a replacement can introduce a duplicate; existing ones are not this edit's business
        if texts.count(text) > 1:
            raise _refuse(
                f"the edit would leave two identical criteria: {text!r}",
                "an identical pair cannot be told apart by `task check <id> <text>` and one of them is noise",
                f"reword the replacement, or {REMOVE_FLAG} the duplicate",
            )
    for text in additions:
        _check_text("--acceptance", text, "write each criterion on one line, one --acceptance per criterion")
    present = {c.text for c in result}
    for text in additions:
        if text not in present:
            result.append(Criterion(text=text))
            present.add(text)
    if edit.active and len(result) < minimum:
        raise _refuse(
            f"this edit would leave {len(result)} acceptance criteria, but at least {minimum} are required",
            "a ticket needs enough criteria to be accepted, and the same minimum is enforced when it closes",
            'add replacements with --acceptance "<criterion>" in the same command, '
            'or pass --skip-acceptance "<reason>" if fewer is genuinely right',
        )
    return result
