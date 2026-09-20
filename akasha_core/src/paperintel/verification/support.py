"""verification.support — claim support-state transitions (P08, doc 03 §1.5).

The frozen support states are UNVERIFIED / SUPPORTED / PARTIALLY_SUPPORTED
/ DISPUTED / UNSUPPORTED / RETRACTED / INSUFFICIENT_EVIDENCE. Verifiers
produce verdicts; THIS module is the single place that maps a verdict set
to a support state — so the policy is auditable and cannot drift between
call sites.

Transition policy (documented, deterministic):

- any FAIL from contradiction/consensus  → DISPUTED (mutual disagreement)
- any FAIL from existence/citation/numeric/scope/falsification
                                         → UNSUPPORTED
- any WARN (no FAIL)                     → PARTIALLY_SUPPORTED
- all applicable verifiers PASS          → SUPPORTED
- only INCONCLUSIVE verdicts             → INSUFFICIENT_EVIDENCE
- no verifier ran                        → UNVERIFIED (unchanged)

RETRACTED is never produced by the engine: it is reserved for explicit
correction/supersession (doc 03 §1.5), never for an automatic verdict.
"""

from __future__ import annotations

from paperintel.schemas.enums import SupportState, VerifierType

#: Verifiers whose FAIL means "the agents disagree", not "the claim is
#: unsupported by the paper".
_DISPUTE_VERIFIERS = frozenset({VerifierType.CONTRADICTION, VerifierType.INDEPENDENT_CONSENSUS})

#: Verifiers whose FAIL means "the claim is not supported by evidence".
_UNSUPPORTED_VERIFIERS = frozenset(
    {
        VerifierType.EVIDENCE_EXISTENCE,
        VerifierType.CITATION,
        VerifierType.NUMERIC,
        VerifierType.CLAIM_SCOPE,
        VerifierType.FALSIFICATION,
    }
)


def support_state_for(verdicts: dict[VerifierType, str]) -> SupportState:
    """Map a claim's verifier verdicts to its canonical support state."""
    if not verdicts:
        return SupportState.UNVERIFIED

    failed = [vt for vt, verdict in verdicts.items() if verdict == "FAIL"]
    warned = [vt for vt, verdict in verdicts.items() if verdict == "WARN"]
    passed = [vt for vt, verdict in verdicts.items() if verdict == "PASS"]

    if any(vt in _DISPUTE_VERIFIERS for vt in failed):
        return SupportState.DISPUTED
    if any(vt in _UNSUPPORTED_VERIFIERS for vt in failed):
        return SupportState.UNSUPPORTED
    if failed:
        # An unclassified verifier failed — treat as not supported; it is
        # never silently ignored.
        return SupportState.UNSUPPORTED
    if warned:
        return SupportState.PARTIALLY_SUPPORTED
    if passed:
        return SupportState.SUPPORTED
    return SupportState.INSUFFICIENT_EVIDENCE
