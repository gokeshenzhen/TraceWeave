#!/usr/bin/env python3
"""Internal exact-only Verdi NPI worker used by the LSF transport."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.npi_lsf import (  # type: ignore
        WorkerError,
        execute_worker_request,
        parse_worker_request_bytes,
        write_worker_response,
    )
else:
    from .npi_lsf import (
        WorkerError,
        execute_worker_request,
        parse_worker_request_bytes,
        write_worker_response,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    os.environ["TRACEWEAVE_NPI_WORKER"] = "1"
    args = _parse_args(argv)
    request_path = Path(args.request)
    response_path = Path(args.response)

    try:
        request = parse_worker_request_bytes(request_path.read_bytes())
    except Exception:  # noqa: BLE001
        try:
            write_worker_response(
                response_path,
                WorkerError(error_code="request_invalid", stage="request"),
            )
        except Exception:  # noqa: BLE001
            return 2
        return 2

    response = execute_worker_request(request)
    try:
        write_worker_response(response_path, response)
    except Exception:  # noqa: BLE001
        return 2
    return 0 if response.status != "error" else 2


if __name__ == "__main__":
    raise SystemExit(main())
