"""Compatibility import; the implementation lives in the frozen web namespace."""

from paperintel.web.ui import render_operations_view, render_paper_view

__all__ = ["render_operations_view", "render_paper_view"]
