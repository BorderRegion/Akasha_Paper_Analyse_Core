"""Task state machine conformance (P00 gate check P00-C14).

Spec doc 03 §9: valid transitions listed; invalid transitions must raise a
domain error and be tested.
"""

from __future__ import annotations

import pytest

from paperintel.errors import InvalidStateTransitionError
from paperintel.schemas.enums import (
    TASK_TRANSITIONS,
    TERMINAL_TASK_STATES,
    TaskState,
    is_valid_task_transition,
    validate_task_transition,
)

#: Every transition explicitly listed in spec doc 03 §9.
SPEC_LISTED_TRANSITIONS = [
    (TaskState.PENDING, TaskState.QUEUED),
    (TaskState.QUEUED, TaskState.RUNNING),
    (TaskState.RUNNING, TaskState.SUCCEEDED),
    (TaskState.RUNNING, TaskState.SUCCEEDED_WITH_WARNINGS),
    (TaskState.RUNNING, TaskState.RETRYING),
    (TaskState.RETRYING, TaskState.QUEUED),
    (TaskState.RUNNING, TaskState.WAITING),
    (TaskState.WAITING, TaskState.QUEUED),
    (TaskState.RUNNING, TaskState.FAILED),
    (TaskState.PENDING, TaskState.CANCELLED),
    (TaskState.QUEUED, TaskState.CANCELLED),
    (TaskState.RUNNING, TaskState.CANCELLED),
    (TaskState.WAITING, TaskState.CANCELLED),
    (TaskState.PENDING, TaskState.SKIPPED),
]

INVALID_TRANSITIONS = [
    (TaskState.SUCCEEDED, TaskState.RUNNING),
    (TaskState.SUCCEEDED, TaskState.FAILED),
    (TaskState.SUCCEEDED_WITH_WARNINGS, TaskState.RUNNING),
    (TaskState.FAILED, TaskState.QUEUED),
    (TaskState.CANCELLED, TaskState.QUEUED),
    (TaskState.SKIPPED, TaskState.QUEUED),
    (TaskState.PENDING, TaskState.RUNNING),  # must be queued first
    (TaskState.PENDING, TaskState.SUCCEEDED),
    (TaskState.QUEUED, TaskState.SUCCEEDED),  # must run first
    (TaskState.RUNNING, TaskState.PENDING),
    (TaskState.WAITING, TaskState.RUNNING),  # re-queue first
    (TaskState.RETRYING, TaskState.RUNNING),  # re-queue first
    (TaskState.RUNNING, TaskState.SKIPPED),
    (TaskState.SUCCEEDED, TaskState.SUCCEEDED),
]


@pytest.mark.parametrize(("src", "dst"), SPEC_LISTED_TRANSITIONS)
def test_spec_listed_transitions_are_valid(src: TaskState, dst: TaskState) -> None:
    assert is_valid_task_transition(src, dst)
    validate_task_transition(src, dst)  # must not raise


@pytest.mark.parametrize(("src", "dst"), INVALID_TRANSITIONS)
def test_invalid_transitions_raise_domain_error(src: TaskState, dst: TaskState) -> None:
    assert not is_valid_task_transition(src, dst)
    with pytest.raises(InvalidStateTransitionError) as excinfo:
        validate_task_transition(src, dst)
    error = excinfo.value
    assert error.code == "INTERNAL_002"
    assert error.details["from_state"] == src.value
    assert error.details["to_state"] == dst.value


def test_terminal_states_have_no_exits() -> None:
    expected_terminals = {
        TaskState.SUCCEEDED,
        TaskState.SUCCEEDED_WITH_WARNINGS,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.SKIPPED,
    }
    assert TERMINAL_TASK_STATES == expected_terminals
    for state in expected_terminals:
        assert TASK_TRANSITIONS[state] == frozenset()


def test_every_state_is_in_transition_table() -> None:
    assert set(TASK_TRANSITIONS) == set(TaskState)


def test_no_self_transitions() -> None:
    for state, targets in TASK_TRANSITIONS.items():
        assert state not in targets, f"{state} allows a self-transition"


def test_blocked_state_is_enterable_and_exitable() -> None:
    """BLOCKED is a frozen state (doc 02 §8) and doc 04 §14 exposes blocked
    tasks; it must be reachable and resumable/cancellable."""
    assert is_valid_task_transition(TaskState.RUNNING, TaskState.BLOCKED)
    assert is_valid_task_transition(TaskState.BLOCKED, TaskState.QUEUED)
    assert is_valid_task_transition(TaskState.BLOCKED, TaskState.CANCELLED)
    assert is_valid_task_transition(TaskState.BLOCKED, TaskState.FAILED)


def test_retry_exhaustion_path_exists() -> None:
    """Doc 05 P05 requires 'retry exhaustion': RETRYING must be able to FAIL."""
    assert is_valid_task_transition(TaskState.RETRYING, TaskState.FAILED)
