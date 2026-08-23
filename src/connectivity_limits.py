"""Shared internal work/output limits for connectivity queries.

The public MCP signatures intentionally do not expose these implementation
budgets.  Every backend must nevertheless publish when a returned load list is
only a bounded prefix, so callers never mistake resource protection for an
exhaustive enumeration.
"""

from __future__ import annotations


DEFAULT_LOAD_OUTPUT_LIMIT = 256
DEFAULT_NPI_LOAD_HANDLE_LIMIT = 16_384
DEFAULT_NPI_LOAD_BOUNDARY_STATE_LIMIT = 64
