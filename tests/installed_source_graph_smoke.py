"""Exercise the optional Source Graph worker from an installed wheel."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import tempfile

from traceweave_mcp._runtime.src.slang_connectivity_projector import (
    SLANG_FRONTEND_NAME,
)
from traceweave_mcp._runtime.src.source_graph_contract import (
    BoundaryMode,
    CompileInputManifest,
    ConnectivityTarget,
    CoverageBoundary,
    QueryOperation,
    RequestedCone,
    SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
    SourceGraphArtifactIdentity,
    SourceGraphArtifactScope,
    SourceGraphBuildRequest,
    SourceGraphBuildScope,
    SourceGraphIdentity,
    SourceGraphQueryIdentity,
)
from traceweave_mcp._runtime.src.source_graph_runtime import (
    IsolatedSourceGraphProcessRunner,
    PrepareStatus,
)


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="traceweave-installed-source-graph-") as temp:
        temp_path = Path(temp)
        source = temp_path / "simple.sv"
        source.write_text(
            """\
module top(input logic a, output logic y);
    assign y = a;
endmodule
""",
            encoding="utf-8",
        )
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        scope = SourceGraphBuildScope(
            design="packaged_worker_smoke",
            top="top",
            target=ConnectivityTarget(
                operation=QueryOperation.DRIVER,
                signal_path="top.y",
            ),
            hierarchy_ancestors=("top",),
            requested_cone=RequestedCone(
                operation=QueryOperation.DRIVER,
                max_hops=2,
                instance_paths=("top",),
            ),
            coverage_boundary=CoverageBoundary(
                mode=BoundaryMode.EXPLICIT,
                instance_paths=("top",),
            ),
        )
        identity = SourceGraphIdentity(
            compile_inputs=CompileInputManifest(
                fingerprint=digest,
                ordered_inputs=(str(source),),
                ordered_options=(),
                ordered_tops=("top",),
                inputs_complete=True,
                options_complete=True,
                tops_complete=True,
            ),
            frontend_name=SLANG_FRONTEND_NAME,
            frontend_version="11.0.0",
        )
        artifact = SourceGraphArtifactIdentity(
            source=identity,
            scope=SourceGraphArtifactScope.from_build_scope(
                scope,
                hierarchy_snapshot_sha256=hashlib.sha256(b"hierarchy").hexdigest(),
            ),
            compile_snapshot_sha256=hashlib.sha256(b"compile").hexdigest(),
            adapter_version="packaging-smoke",
            worker_protocol_version=SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
        )
        request = SourceGraphBuildRequest(
            identity=identity,
            scope=scope,
            artifact=artifact,
            query=SourceGraphQueryIdentity.from_build_scope(scope),
        )
        result = await IsolatedSourceGraphProcessRunner(
            staging_directory=temp_path,
        ).run(
            request,
            timeout_seconds=30.0,
            cancel_event=asyncio.Event(),
        )

    assert result.status is PrepareStatus.READY, (result.status, result.blocker)
    assert result.ir_json_bytes
    print(
        "Installed Source Graph worker passed with "
        f"{len(result.ir_json_bytes)} IR bytes"
    )


if __name__ == "__main__":
    asyncio.run(main())
