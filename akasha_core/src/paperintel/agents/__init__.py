"""agents package — importing it registers the built-in agent suite."""

from paperintel.agents import builtin  # noqa: F401  (registration side effect)

__all__ = ["builtin"]
