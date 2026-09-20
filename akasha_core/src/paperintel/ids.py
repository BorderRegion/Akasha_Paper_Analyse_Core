"""Public identifier factories (spec doc 02 §2 — FROZEN).

IDs are opaque stable strings with a frozen entity-kind prefix and a ULID
payload (time-ordered, 128-bit, Crockford base32). Sequential database IDs are
never exposed as public identifiers.

Frozen prefixes::

    pap_    Paper
    pver_   PaperVersion
    ast_    Asset
    ev_     Evidence
    clm_    Claim
    ver_    Verification
    ent_    Entity
    rel_    Relation
    tag_    Tag
    col_    Collection

    job_    Job
    tsk_    Task
    run_    AnalysisRun
    call_   ModelCall
    trc_    Trace

    prv_    Provider
    mdl_    Model
    prm_    PromptVersion
"""

from __future__ import annotations

import re
from enum import StrEnum

from ulid import ULID

# Crockford base32 alphabet used by ULID string encoding (I, L, O, U excluded).
_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_LEN = 26


class IdPrefix(StrEnum):
    """Frozen public ID prefixes (spec doc 02 §2)."""

    PAPER = "pap_"
    PAPER_VERSION = "pver_"
    ASSET = "ast_"
    EVIDENCE = "ev_"
    CLAIM = "clm_"
    VERIFICATION = "ver_"
    ENTITY = "ent_"
    RELATION = "rel_"
    TAG = "tag_"
    COLLECTION = "col_"
    JOB = "job_"
    TASK = "tsk_"
    RUN = "run_"
    MODEL_CALL = "call_"
    TRACE = "trc_"
    PROVIDER = "prv_"
    MODEL = "mdl_"
    PROMPT_VERSION = "prm_"
    # Workbench-local records (frontend spec docs/06 「新增持久数据」): these are
    # UI-owned rows, registered here so every public ID keeps one scheme.
    UI_NOTE = "uin_"
    UI_REVIEW_DECISION = "urd_"
    UI_SAVED_SEARCH = "uss_"
    UI_IMPORT_BATCH = "uib_"
    UI_IMPORT_ITEM = "uit_"
    UI_OPERATION = "uop_"


_ID_RE = re.compile(
    r"^(?P<prefix>"
    + "|".join(re.escape(p.value) for p in IdPrefix)
    + r")"
    + f"(?P<payload>[0-9A-HJKMNP-TV-Z]{{{_ULID_LEN}}})$"
)


class IdError(ValueError):
    """Raised when a string is not a valid PaperIntel public ID."""


def new_id(prefix: IdPrefix) -> str:
    """Generate a new globally unique public ID for the given entity kind."""
    return f"{prefix.value}{ULID()}"


def new_paper_id() -> str:
    return new_id(IdPrefix.PAPER)


def new_paper_version_id() -> str:
    return new_id(IdPrefix.PAPER_VERSION)


def new_asset_id() -> str:
    return new_id(IdPrefix.ASSET)


def new_evidence_id() -> str:
    return new_id(IdPrefix.EVIDENCE)


def new_claim_id() -> str:
    return new_id(IdPrefix.CLAIM)


def new_verification_id() -> str:
    return new_id(IdPrefix.VERIFICATION)


def new_entity_id() -> str:
    return new_id(IdPrefix.ENTITY)


def new_relation_id() -> str:
    return new_id(IdPrefix.RELATION)


def new_tag_id() -> str:
    return new_id(IdPrefix.TAG)


def new_collection_id() -> str:
    return new_id(IdPrefix.COLLECTION)


def new_job_id() -> str:
    return new_id(IdPrefix.JOB)


def new_task_id() -> str:
    return new_id(IdPrefix.TASK)


def new_run_id() -> str:
    return new_id(IdPrefix.RUN)


def new_model_call_id() -> str:
    return new_id(IdPrefix.MODEL_CALL)


def new_trace_id() -> str:
    return new_id(IdPrefix.TRACE)


def new_provider_id() -> str:
    return new_id(IdPrefix.PROVIDER)


def new_model_id() -> str:
    return new_id(IdPrefix.MODEL)


def new_prompt_version_id() -> str:
    return new_id(IdPrefix.PROMPT_VERSION)


def parse_id(value: str) -> tuple[IdPrefix, str]:
    """Split a public ID into (prefix, ULID payload).

    Raises:
        IdError: if the value is not a well-formed public ID.
    """
    if not isinstance(value, str):
        raise IdError(f"ID must be a string, got {type(value).__name__}")
    match = _ID_RE.match(value)
    if match is None:
        raise IdError(f"malformed public ID: {value!r}")
    return IdPrefix(match.group("prefix")), match.group("payload")


def is_valid_id(value: str, expected_prefix: IdPrefix | None = None) -> bool:
    """Return True when ``value`` is a well-formed public ID.

    When ``expected_prefix`` is given the ID must also carry that prefix.
    """
    try:
        prefix, _payload = parse_id(value)
    except IdError:
        return False
    return expected_prefix is None or prefix is expected_prefix


def validate_id(value: str, expected_prefix: IdPrefix) -> str:
    """Return ``value`` unchanged after validating prefix and format.

    Raises:
        IdError: on malformed IDs or prefix mismatch.
    """
    prefix, _payload = parse_id(value)
    if prefix is not expected_prefix:
        raise IdError(
            f"ID {value!r} has prefix {prefix.value!r}, expected {expected_prefix.value!r}"
        )
    return value


__all__ = [
    "IdError",
    "IdPrefix",
    "is_valid_id",
    "new_asset_id",
    "new_claim_id",
    "new_collection_id",
    "new_entity_id",
    "new_evidence_id",
    "new_id",
    "new_job_id",
    "new_model_call_id",
    "new_model_id",
    "new_paper_id",
    "new_paper_version_id",
    "new_prompt_version_id",
    "new_provider_id",
    "new_relation_id",
    "new_run_id",
    "new_tag_id",
    "new_task_id",
    "new_trace_id",
    "new_verification_id",
    "parse_id",
    "validate_id",
]


def new_ui_note_id() -> str:
    return new_id(IdPrefix.UI_NOTE)


def new_ui_review_decision_id() -> str:
    return new_id(IdPrefix.UI_REVIEW_DECISION)


def new_ui_saved_search_id() -> str:
    return new_id(IdPrefix.UI_SAVED_SEARCH)


def new_ui_import_batch_id() -> str:
    return new_id(IdPrefix.UI_IMPORT_BATCH)


def new_ui_import_item_id() -> str:
    return new_id(IdPrefix.UI_IMPORT_ITEM)


def new_ui_operation_id() -> str:
    return new_id(IdPrefix.UI_OPERATION)
