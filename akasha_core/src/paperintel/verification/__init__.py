"""verification package — the verification engine (P08, doc 01 §10).

Runs the frozen verifier modules over ledger claims and drives claim
support states (doc 03 §1.5). Importing the package registers the
verifier suite.
"""

from paperintel.verification import verifiers  # noqa: F401  (registration)

__all__ = ["verifiers"]
