"""ID factory tests (P00 gate check P00-C11)."""

from __future__ import annotations

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from paperintel.ids import (
    IdError,
    IdPrefix,
    is_valid_id,
    new_id,
    parse_id,
    validate_id,
)

FACTORIES = {
    IdPrefix.PAPER: "new_paper_id",
    IdPrefix.PAPER_VERSION: "new_paper_version_id",
    IdPrefix.ASSET: "new_asset_id",
    IdPrefix.EVIDENCE: "new_evidence_id",
    IdPrefix.CLAIM: "new_claim_id",
    IdPrefix.VERIFICATION: "new_verification_id",
    IdPrefix.ENTITY: "new_entity_id",
    IdPrefix.RELATION: "new_relation_id",
    IdPrefix.TAG: "new_tag_id",
    IdPrefix.COLLECTION: "new_collection_id",
    IdPrefix.JOB: "new_job_id",
    IdPrefix.TASK: "new_task_id",
    IdPrefix.RUN: "new_run_id",
    IdPrefix.MODEL_CALL: "new_model_call_id",
    IdPrefix.TRACE: "new_trace_id",
    IdPrefix.PROVIDER: "new_provider_id",
    IdPrefix.MODEL: "new_model_id",
    IdPrefix.PROMPT_VERSION: "new_prompt_version_id",
}


#: Prefixes added by the WORKBENCH surface (frontend design 1.0.0 docs/06
#: §新增持久数据: "新增ID前缀集中登记在UI命名空间，不改变旧ID规则").
#: They are listed explicitly so an accidental new prefix still fails this test.
UI_NAMESPACE_PREFIXES = {
    "uin_",  # personal note
    "urd_",  # user review decision
    "uss_",  # saved search
    "uib_",  # UI import batch
    "uit_",  # UI import item
    "uop_",  # UI operation request
}


def test_frozen_prefix_set_matches_spec() -> None:
    """The core prefixes from spec doc 02 §2 are unchanged, and the only
    additions are the registered workbench namespace."""
    expected = {
        "pap_",
        "pver_",
        "ast_",
        "ev_",
        "clm_",
        "ver_",
        "ent_",
        "rel_",
        "tag_",
        "col_",
        "job_",
        "tsk_",
        "run_",
        "call_",
        "trc_",
        "prv_",
        "mdl_",
        "prm_",
    }
    actual = {p.value for p in IdPrefix}
    assert expected <= actual, f"a frozen core prefix disappeared: {sorted(expected - actual)}"
    assert actual - expected == UI_NAMESPACE_PREFIXES, (
        "only the workbench namespace may extend the prefix set"
    )


@pytest.mark.parametrize("prefix", list(IdPrefix))
def test_factory_format_and_roundtrip(prefix: IdPrefix) -> None:
    value = new_id(prefix)
    assert value.startswith(prefix.value)
    parsed_prefix, payload = parse_id(value)
    assert parsed_prefix is prefix
    assert len(payload) == 26
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", payload)
    assert is_valid_id(value, prefix)
    assert validate_id(value, prefix) == value


@pytest.mark.parametrize("prefix", list(IdPrefix))
def test_factory_uniqueness(prefix: IdPrefix) -> None:
    values = {new_id(prefix) for _ in range(500)}
    assert len(values) == 500


def test_ids_are_time_ordered() -> None:
    ids = [new_id(IdPrefix.PAPER) for _ in range(100)]
    assert ids == sorted(ids)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "pap_",
        "pap_tooshort",
        "unknown_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "pap_01ARZ3NDEKTSV4RRFFQ69G5FAVX",  # 27 chars
        "PAP_01ARZ3NDEKTSV4RRFFQ69G5FAV",  # uppercase prefix
        "pap_01ARZ3NDEKTSV4RRFFQ69G5FAI",  # I not in Crockford alphabet
        "pap_01ARZ3NDEKTSV4RRFFQ69G5FAL",  # L not in Crockford alphabet
        "ev_example",  # spec template placeholder: not a strict ID
        "42",
    ],
)
def test_parse_rejects_malformed(bad: str) -> None:
    with pytest.raises(IdError):
        parse_id(bad)


def test_parse_rejects_non_string() -> None:
    with pytest.raises(IdError):
        parse_id(42)  # type: ignore[arg-type]


def test_validate_id_prefix_mismatch() -> None:
    value = new_id(IdPrefix.PAPER)
    with pytest.raises(IdError, match="expected"):
        validate_id(value, IdPrefix.CLAIM)


def test_is_valid_id_without_prefix() -> None:
    assert is_valid_id(new_id(IdPrefix.TRACE))
    assert not is_valid_id("nonsense")


@given(st.sampled_from(list(IdPrefix)))
@settings(max_examples=50, deadline=None)
def test_factory_ids_always_valid(prefix: IdPrefix) -> None:
    assert is_valid_id(new_id(prefix), prefix)


def test_all_factories_exist_and_work() -> None:
    import paperintel.ids as ids_module

    for prefix, factory_name in FACTORIES.items():
        factory = getattr(ids_module, factory_name)
        value = factory()
        assert is_valid_id(value, prefix)
