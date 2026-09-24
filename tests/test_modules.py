"""modules.py — the product-module taxonomy: load/parse, labels, and the classify-time
keyword-overlap inference heuristic."""

from __future__ import annotations

import pytest

from tasklib.config import ConfigError
from tasklib.modules import (
    LABEL_PREFIX,
    ModuleTaxonomy,
    ProductModule,
    assigned_module_names,
    infer_module,
    label_for,
    load_taxonomy,
)


class _FakeConfig:
    def __init__(self, data: dict) -> None:
        self.data = data


# ── load_taxonomy: present / absent / malformed ─────────────────────────────────────


def test_load_taxonomy_absent_is_not_configured():
    taxonomy = load_taxonomy(_FakeConfig({}))
    assert taxonomy.configured is False
    assert taxonomy.modules == ()


def test_load_taxonomy_present_parses_name_and_description():
    taxonomy = load_taxonomy(
        _FakeConfig(
            {
                "modules": [
                    {"name": "classify", "description": "message classification"},
                    {"name": "backends"},
                ]
            }
        )
    )
    assert taxonomy.configured is True
    assert taxonomy.names() == ("classify", "backends")
    assert taxonomy.get("classify") == ProductModule("classify", "message classification")
    assert taxonomy.get("backends") == ProductModule("backends", "")
    assert taxonomy.get("no-such-module") is None


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-list",
        [{"description": "missing name"}],
        [{"name": ""}],
        [{"name": "Has Uppercase"}],
        [{"name": "-starts-with-hyphen"}],
        [{"name": "dup"}, {"name": "dup"}],
        [{"name": "x", "description": 123}],
    ],
)
def test_load_taxonomy_malformed_raises_config_error(raw):
    with pytest.raises(ConfigError):
        load_taxonomy(_FakeConfig({"modules": raw}))


def test_load_taxonomy_empty_list_is_not_configured():
    # An explicit but empty `modules: []` reads the same as absent — no taxonomy adopted yet.
    taxonomy = load_taxonomy(_FakeConfig({"modules": []}))
    assert taxonomy.configured is False


# ── labels ───────────────────────────────────────────────────────────────────────────


def test_label_for_shape():
    assert label_for("classify") == f"{LABEL_PREFIX}classify" == "module:classify"


def test_assigned_module_names_extracts_from_labels():
    labels = ["agent", "module:classify", "sp:3", "module:backends"]
    assert assigned_module_names(labels) == {"classify", "backends"}


def test_assigned_module_names_empty_when_none_present():
    assert assigned_module_names(["agent", "sp:3"]) == set()


# ── infer_module: the classify-time heuristic ───────────────────────────────────────


_TAXONOMY = ModuleTaxonomy(
    modules=(
        ProductModule("classify", "message classification fallback chain"),
        ProductModule("backends", "github issues linear adapters"),
    )
)


def test_infer_module_matches_on_name_mention():
    assert infer_module(_TAXONOMY, "fix a bug in the classify fallback chain") == "classify"


def test_infer_module_matches_on_description_keywords():
    assert infer_module(_TAXONOMY, "the linear adapter needs a retry on 429") == "backends"


def test_infer_module_no_signal_returns_none():
    assert infer_module(_TAXONOMY, "completely unrelated topic about lunch") is None


def test_infer_module_tie_returns_none():
    tied = ModuleTaxonomy(
        modules=(ProductModule("alpha", "shared keyword"), ProductModule("beta", "shared keyword"))
    )
    assert infer_module(tied, "shared keyword shows up here") is None


def test_infer_module_empty_taxonomy_returns_none():
    assert infer_module(ModuleTaxonomy(), "classify this message please") is None


def test_infer_module_empty_text_returns_none():
    assert infer_module(_TAXONOMY, "") is None
