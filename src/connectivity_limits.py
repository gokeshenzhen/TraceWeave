"""Shared internal work/output limits for connectivity queries.

The public MCP signatures intentionally do not expose these implementation
budgets. Every backend must nevertheless publish when returned driver/load
facts are only a bounded prefix, so callers never mistake resource protection
for an exhaustive search.
"""

from __future__ import annotations


DEFAULT_LOAD_OUTPUT_LIMIT = 256
DEFAULT_NPI_LOAD_HANDLE_LIMIT = 16_384
DEFAULT_NPI_LOAD_BOUNDARY_STATE_LIMIT = 64

# NPI's native fan-in API otherwise materializes the whole combinational cone
# before Python can slice its output. The registered FAN_IN callback admits at
# most this many native traversal states and prunes every later branch. The
# returned terminal set retains the historical 32-branch public cap.
DEFAULT_DRIVER_OUTPUT_LIMIT = 32
DEFAULT_NPI_DRIVER_STATE_LIMIT = 4_096
