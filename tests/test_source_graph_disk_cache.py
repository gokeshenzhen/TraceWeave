from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import stat

import pytest

from src.connectivity_ir import (
    CONNECTIVITY_IR_VERSION,
    CoverageGap,
    CoverageReport,
    CoverageStatus,
)
from src.source_graph_contract import (
    SourceGraphArtifactIdentity,
    SourceGraphArtifactScopeReceipt,
    compute_source_graph_artifact_key,
)
from src.source_graph_disk_cache import (
    DiskCacheCancelled,
    DiskPublishOutcome,
    DiskValidationOutcome,
    SOURCE_GRAPH_DISK_CACHE_IR,
    SOURCE_GRAPH_DISK_CACHE_MANIFEST,
    SourceGraphDiskCache,
)
from tests.connectivity_ir_fixtures import build_hand_ir
from tests.test_source_graph_runtime import _request


def _identity(label: str = "disk") -> SourceGraphArtifactIdentity:
    return _request(label=label).artifact_identity


def _receipt(
    identity: SourceGraphArtifactIdentity,
    *,
    status: CoverageStatus = CoverageStatus.COMPLETE,
    gap_codes: tuple[str, ...] = (),
) -> SourceGraphArtifactScopeReceipt:
    return SourceGraphArtifactScopeReceipt(
        scope=identity.scope,
        coverage_status=status,
        gap_codes=gap_codes,
    )


def _canonical_json(value) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _manifest_path(
    store: SourceGraphDiskCache, identity: SourceGraphArtifactIdentity
) -> Path:
    digest = compute_source_graph_artifact_key(identity).digest
    return store.entry_path(digest) / SOURCE_GRAPH_DISK_CACHE_MANIFEST


def _ir_path(
    store: SourceGraphDiskCache, identity: SourceGraphArtifactIdentity
) -> Path:
    digest = compute_source_graph_artifact_key(identity).digest
    return store.entry_path(digest) / SOURCE_GRAPH_DISK_CACHE_IR


def _publish_hand(store: SourceGraphDiskCache, identity: SourceGraphArtifactIdentity):
    ir = build_hand_ir()
    result = store.publish(identity, ir.to_json_bytes(), _receipt(identity))
    assert result.published
    return result


def _rewrite_manifest(store, identity, mutator) -> None:
    path = _manifest_path(store, identity)
    value = json.loads(path.read_bytes())
    mutator(value)
    path.write_bytes(_canonical_json(value))
    path.chmod(0o600)


def _multiprocess_publish(root, identity, payload, receipt, queue) -> None:
    store = SourceGraphDiskCache(root)
    result = store.publish(identity, payload, receipt)
    queue.put(result.outcome.value)


def test_constructor_and_empty_exact_lookup_are_lazy(tmp_path):
    cache_root = tmp_path / "cache"
    store = SourceGraphDiskCache(cache_root)

    assert not cache_root.exists()
    assert store.lookup(_identity()).outcome is DiskValidationOutcome.NOT_FOUND
    assert not cache_root.exists()


def test_exact_round_trip_uses_canonical_ir_manifest_and_private_permissions(tmp_path):
    cache_root = tmp_path / "cache"
    store = SourceGraphDiskCache(cache_root)
    identity = _identity()
    ir = build_hand_ir()

    published = store.publish(identity, ir.to_json_bytes(), _receipt(identity))
    lookup = store.lookup(identity)
    snapshot = store.maintenance_snapshot()

    assert published.outcome is DiskPublishOutcome.PUBLISHED
    assert lookup.outcome is DiskValidationOutcome.HIT
    assert lookup.artifact is not None
    assert lookup.artifact.identity == identity
    assert lookup.artifact.ir_json_bytes == ir.to_json_bytes()
    assert lookup.artifact.ir_fingerprint_sha256 == ir.fingerprint_sha256()
    assert lookup.artifact.scope_receipt == _receipt(identity)
    assert lookup.bytes_read == lookup.artifact.entry_bytes
    assert snapshot.entry_count == 1
    assert snapshot.disk_bytes == lookup.artifact.entry_bytes
    assert snapshot.unsafe_entry_count == 0

    manifest = json.loads(_manifest_path(store, identity).read_bytes())
    assert manifest["completed"] is True
    assert manifest["artifact"]["identity"] == identity.to_dict()
    assert manifest["artifact"]["digest"] == (
        compute_source_graph_artifact_key(identity).digest
    )
    assert manifest["ir"] == {
        "file": SOURCE_GRAPH_DISK_CACHE_IR,
        "schema_version": CONNECTIVITY_IR_VERSION,
        "sha256": ir.fingerprint_sha256(),
        "size_bytes": len(ir.to_json_bytes()),
    }
    assert "query_identity" not in json.dumps(manifest, sort_keys=True)

    for path in [store.namespace_root, *store.namespace_root.rglob("*")]:
        info = path.lstat()
        assert info.st_uid == os.getuid()
        assert not info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        if path.is_dir():
            assert stat.S_IMODE(info.st_mode) == 0o700
        else:
            assert stat.S_ISREG(info.st_mode)
            assert stat.S_IMODE(info.st_mode) == 0o600


def test_partial_coverage_and_exclusions_are_preserved_honestly(tmp_path):
    identity = _identity()
    identity = replace(
        identity,
        scope=replace(
            identity.scope,
            coverage_boundary=replace(
                identity.scope.coverage_boundary,
                objective_exclusions=("dpi_runtime",),
            ),
        ),
    )
    ir = build_hand_ir()
    gap = CoverageGap(
        code="dpi_runtime",
        message="runtime DPI behavior is excluded",
        impact=CoverageStatus.INCONCLUSIVE,
        scopes=("sg_top",),
    )
    ir = replace(
        ir,
        coverage=CoverageReport(
            status=CoverageStatus.INCONCLUSIVE,
            files_total=ir.coverage.files_total,
            files_projected=ir.coverage.files_projected,
            gaps=(gap,),
        ),
    )
    receipt = _receipt(
        identity,
        status=CoverageStatus.INCONCLUSIVE,
        gap_codes=("dpi_runtime",),
    )
    store = SourceGraphDiskCache(tmp_path / "cache")

    assert store.publish(identity, ir.to_json_bytes(), receipt).published
    hit = store.lookup(identity)

    assert hit.hit
    assert hit.artifact is not None
    assert hit.artifact.scope_receipt.coverage_status is CoverageStatus.INCONCLUSIVE
    assert hit.artifact.scope_receipt.gap_codes == ("dpi_runtime",)
    manifest = json.loads(_manifest_path(store, identity).read_bytes())
    assert manifest["coverage"]["objective_exclusions"] == ["dpi_runtime"]
    assert manifest["coverage"]["status"] == "inconclusive"


def test_query_identity_never_changes_or_enters_artifact_storage(tmp_path):
    first_request = _request(label="query_identity")
    second_request = replace(
        first_request,
        query=replace(first_request.query_identity, max_depth=32),
    )
    assert first_request.artifact_identity == second_request.artifact_identity
    assert compute_source_graph_artifact_key(
        first_request.artifact_identity
    ) == compute_source_graph_artifact_key(second_request.artifact_identity)

    store = SourceGraphDiskCache(tmp_path / "cache")
    _publish_hand(store, first_request.artifact_identity)
    assert store.lookup(second_request.artifact_identity).hit


@pytest.mark.parametrize(
    "variant",
    (
        "compile_fingerprint",
        "ordered_inputs",
        "ordered_input_order",
        "ordered_options",
        "ordered_tops",
        "compile_snapshot",
        "hierarchy_snapshot",
        "frontend_version",
        "projector_version",
        "ir_schema_version",
        "adapter_version",
        "worker_protocol_version",
        "scope_capabilities",
    ),
)
def test_every_artifact_affecting_identity_change_is_a_direct_miss(tmp_path, variant):
    identity = _identity("identity_base")
    compile_inputs = identity.source.compile_inputs
    source = identity.source
    scope = identity.scope
    if variant == "compile_fingerprint":
        changed = replace(
            identity,
            source=replace(
                source,
                compile_inputs=replace(
                    compile_inputs,
                    fingerprint=hashlib.sha256(b"changed content").hexdigest(),
                ),
            ),
        )
    elif variant == "ordered_inputs":
        changed = replace(
            identity,
            source=replace(
                source,
                compile_inputs=replace(
                    compile_inputs,
                    ordered_inputs=(*compile_inputs.ordered_inputs, "support.svh"),
                ),
            ),
        )
    elif variant == "ordered_input_order":
        changed = replace(
            identity,
            source=replace(
                source,
                compile_inputs=replace(
                    compile_inputs,
                    ordered_inputs=("support.svh", *compile_inputs.ordered_inputs),
                ),
            ),
        )
    elif variant == "ordered_options":
        changed = replace(
            identity,
            source=replace(
                source,
                compile_inputs=replace(
                    compile_inputs,
                    ordered_options=(
                        *compile_inputs.ordered_options,
                        "+define+CHANGED",
                    ),
                ),
            ),
        )
    elif variant == "ordered_tops":
        changed = replace(
            identity,
            source=replace(
                source,
                compile_inputs=replace(
                    compile_inputs,
                    ordered_tops=(*compile_inputs.ordered_tops, "bind_top"),
                ),
            ),
        )
    elif variant == "compile_snapshot":
        changed = replace(
            identity,
            compile_snapshot_sha256=hashlib.sha256(b"compile changed").hexdigest(),
        )
    elif variant == "hierarchy_snapshot":
        changed = replace(
            identity,
            scope=replace(
                scope,
                hierarchy_snapshot_sha256=hashlib.sha256(
                    b"hierarchy changed"
                ).hexdigest(),
            ),
        )
    elif variant == "frontend_version":
        changed = replace(identity, source=replace(source, frontend_version="2.0"))
    elif variant == "projector_version":
        changed = replace(identity, source=replace(source, projector_version="2.0"))
    elif variant == "ir_schema_version":
        changed = replace(identity, source=replace(source, ir_schema_version="2.0"))
    elif variant == "adapter_version":
        changed = replace(identity, adapter_version="test_adapter_4_0")
    elif variant == "worker_protocol_version":
        changed = replace(identity, worker_protocol_version="4.0")
    else:
        changed = replace(
            identity,
            scope=replace(scope, capabilities=scope.capabilities[:2]),
        )

    assert compute_source_graph_artifact_key(identity).digest != (
        compute_source_graph_artifact_key(changed).digest
    )
    store = SourceGraphDiskCache(tmp_path / "cache")
    _publish_hand(store, identity)
    assert store.lookup(changed).outcome is DiskValidationOutcome.NOT_FOUND


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("unknown_format", DiskValidationOutcome.UNKNOWN_FORMAT),
        ("incomplete", DiskValidationOutcome.INCOMPLETE_ENTRY),
        ("artifact_key", DiskValidationOutcome.ARTIFACT_KEY_MISMATCH),
        ("artifact_identity", DiskValidationOutcome.ARTIFACT_IDENTITY_MISMATCH),
        ("build_semantics", DiskValidationOutcome.BUILD_SEMANTICS_MISMATCH),
        ("scope", DiskValidationOutcome.SCOPE_MISMATCH),
        ("snapshot", DiskValidationOutcome.SNAPSHOT_MISMATCH),
        ("version", DiskValidationOutcome.VERSION_MISMATCH),
        ("coverage", DiskValidationOutcome.COVERAGE_RECEIPT_MISMATCH),
        ("ir_size", DiskValidationOutcome.IR_SIZE_MISMATCH),
        ("ir_digest", DiskValidationOutcome.IR_DIGEST_MISMATCH),
        ("unknown_field", DiskValidationOutcome.MANIFEST_INVALID),
    ),
)
def test_manifest_mismatch_is_a_fixed_reason_safe_miss(tmp_path, mutation, expected):
    identity = _identity()
    store = SourceGraphDiskCache(tmp_path / "cache")
    _publish_hand(store, identity)

    def mutate(value):
        if mutation == "unknown_format":
            value["format_version"] = "999"
        elif mutation == "incomplete":
            value["completed"] = False
        elif mutation == "artifact_key":
            value["artifact"]["digest"] = "0" * 64
        elif mutation == "artifact_identity":
            value["artifact"]["identity"]["compile_snapshot_sha256"] = "0" * 64
        elif mutation == "build_semantics":
            value["artifact"]["build_semantics_digest"] = "0" * 64
        elif mutation == "scope":
            value["artifact"]["scope_digest"] = "0" * 64
        elif mutation == "snapshot":
            value["snapshots"]["compile_snapshot_sha256"] = "0" * 64
        elif mutation == "version":
            value["versions"]["worker_protocol"] = "999"
        elif mutation == "coverage":
            value["coverage"]["files_projected"] = 0
        elif mutation == "ir_size":
            value["ir"]["size_bytes"] += 1
        elif mutation == "ir_digest":
            value["ir"]["sha256"] = "0" * 64
        else:
            value["query_identity"] = {"forbidden": True}

    _rewrite_manifest(store, identity, mutate)
    result = store.lookup(identity)

    assert result.outcome is expected
    assert result.artifact is None
    assert result.corrupt


def test_truncated_manifest_and_ir_are_safe_misses_and_repair_on_publish(tmp_path):
    identity = _identity()
    store = SourceGraphDiskCache(tmp_path / "cache")
    _publish_hand(store, identity)
    manifest_path = _manifest_path(store, identity)
    manifest_path.write_bytes(b'{"completed":')

    assert store.lookup(identity).outcome is DiskValidationOutcome.MANIFEST_INVALID
    repaired = store.publish(
        identity, build_hand_ir().to_json_bytes(), _receipt(identity)
    )
    assert repaired.outcome is DiskPublishOutcome.PUBLISHED
    assert store.lookup(identity).hit

    ir_path = _ir_path(store, identity)
    ir_path.write_bytes(ir_path.read_bytes()[:100])
    assert store.lookup(identity).outcome is DiskValidationOutcome.IR_SIZE_MISMATCH
    assert store.publish(
        identity, build_hand_ir().to_json_bytes(), _receipt(identity)
    ).published
    assert store.lookup(identity).hit


@pytest.mark.parametrize(
    ("field", "expected"),
    (
        ("ir_version", DiskValidationOutcome.IR_SCHEMA_MISMATCH),
        ("frontend_version", DiskValidationOutcome.IR_IDENTITY_MISMATCH),
    ),
)
def test_rehashed_but_semantically_invalid_ir_is_rejected(tmp_path, field, expected):
    identity = _identity()
    store = SourceGraphDiskCache(tmp_path / "cache")
    _publish_hand(store, identity)
    ir_path = _ir_path(store, identity)
    ir_value = json.loads(ir_path.read_bytes())
    ir_value[field] = "999"
    altered = _canonical_json(ir_value)
    ir_path.write_bytes(altered)

    def update_ir_receipt(value):
        value["ir"]["sha256"] = hashlib.sha256(altered).hexdigest()
        value["ir"]["size_bytes"] = len(altered)

    _rewrite_manifest(store, identity, update_ir_receipt)
    assert store.lookup(identity).outcome is expected


def test_entry_and_byte_capacity_use_deterministic_specific_eviction(tmp_path):
    identities = tuple(_identity(f"capacity_{index}") for index in range(3))
    store = SourceGraphDiskCache(
        tmp_path / "entries", max_entries=2, max_bytes=1_000_000
    )
    first = _publish_hand(store, identities[0])
    _publish_hand(store, identities[1])
    third = _publish_hand(store, identities[2])
    first_two = sorted(
        identities[:2], key=lambda item: compute_source_graph_artifact_key(item).digest
    )

    assert first.entry_bytes > 0
    assert third.eviction_count == 1
    assert store.lookup(first_two[0]).outcome is DiskValidationOutcome.NOT_FOUND
    assert store.lookup(first_two[1]).hit
    assert store.lookup(identities[2]).hit
    assert store.maintenance_snapshot().entry_count == 2

    byte_store = SourceGraphDiskCache(
        tmp_path / "bytes", max_entries=8, max_bytes=50_000
    )
    _publish_hand(byte_store, identities[0])
    second = _publish_hand(byte_store, identities[1])
    assert second.eviction_count == 1
    assert byte_store.lookup(identities[0]).outcome is DiskValidationOutcome.NOT_FOUND
    assert byte_store.lookup(identities[1]).hit
    assert byte_store.maintenance_snapshot().disk_bytes <= 50_000

    oversize_store = SourceGraphDiskCache(
        tmp_path / "oversize", max_entries=8, max_bytes=1_000
    )
    oversize = oversize_store.publish(
        identities[0], build_hand_ir().to_json_bytes(), _receipt(identities[0])
    )
    assert oversize.outcome is DiskPublishOutcome.BYPASS_CAPACITY
    assert not oversize_store.namespace_root.exists()


def test_concurrent_process_publishers_converge_on_one_completed_entry(tmp_path):
    identity = _identity("multiprocess")
    payload = build_hand_ir().to_json_bytes()
    receipt = _receipt(identity)
    root = tmp_path / "cache"
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(
            target=_multiprocess_publish,
            args=(root, identity, payload, receipt, queue),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    outcomes = {queue.get(timeout=20) for _ in processes}
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    queue.close()

    assert outcomes <= {"published", "already_present"}
    assert "published" in outcomes
    store = SourceGraphDiskCache(root)
    assert store.lookup(identity).hit
    assert store.maintenance_snapshot().entry_count == 1
    assert not tuple((store.namespace_root / "tmp").iterdir())


def test_cancellation_cleans_only_current_temp_and_retry_is_safe(tmp_path):
    identity = _identity("cancel")
    store = SourceGraphDiskCache(tmp_path / "cache")
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 4

    with pytest.raises(DiskCacheCancelled):
        store.publish(
            identity,
            build_hand_ir().to_json_bytes(),
            _receipt(identity),
            cancelled=cancelled,
        )

    assert not store.entry_path(
        compute_source_graph_artifact_key(identity).digest
    ).exists()
    assert not tuple((store.namespace_root / "tmp").iterdir())
    assert store.publish(
        identity, build_hand_ir().to_json_bytes(), _receipt(identity)
    ).published
    assert store.lookup(identity).hit


@pytest.mark.parametrize("kind", ("symlink", "fifo"))
def test_symlink_and_non_regular_ir_are_rejected_without_following(tmp_path, kind):
    identity = _identity(kind)
    store = SourceGraphDiskCache(tmp_path / "cache")
    _publish_hand(store, identity)
    ir_path = _ir_path(store, identity)
    ir_path.unlink()
    external = tmp_path / "external"
    external.write_bytes(b"do not touch")
    if kind == "symlink":
        ir_path.symlink_to(external)
    else:
        os.mkfifo(ir_path, mode=0o600)

    assert store.lookup(identity).outcome is DiskValidationOutcome.UNSAFE_ENTRY
    repaired = store.publish(
        identity, build_hand_ir().to_json_bytes(), _receipt(identity)
    )
    assert repaired.outcome is DiskPublishOutcome.PUBLISHED
    assert external.read_bytes() == b"do not touch"
    assert store.lookup(identity).hit


def test_unknown_entry_content_is_never_recursively_cleaned(tmp_path):
    identity = _identity("unsafe_cleanup")
    store = SourceGraphDiskCache(tmp_path / "cache")
    _publish_hand(store, identity)
    entry = store.entry_path(compute_source_graph_artifact_key(identity).digest)
    unknown = entry / "user-owned-unknown-file"
    unknown.write_text("preserve", encoding="utf-8")
    unknown.chmod(0o600)

    assert store.lookup(identity).outcome is DiskValidationOutcome.UNSAFE_ENTRY
    result = store.publish(
        identity, build_hand_ir().to_json_bytes(), _receipt(identity)
    )
    assert result.outcome is DiskPublishOutcome.UNSAFE_EXISTING_ENTRY
    assert unknown.read_text(encoding="utf-8") == "preserve"


def test_path_traversal_and_symlinked_namespace_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        SourceGraphDiskCache(tmp_path / "nested" / "..")
    store = SourceGraphDiskCache(tmp_path / "cache")
    for unsafe in ("../escape", "A" * 64, "short"):
        with pytest.raises(ValueError):
            store.entry_path(unsafe)

    real_root = tmp_path / "real"
    real_root.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)
    linked = SourceGraphDiskCache(linked_root)
    identity = _identity("linked")
    assert linked.lookup(identity).outcome is DiskValidationOutcome.UNSAFE_NAMESPACE
    assert (
        linked.publish(
            identity, build_hand_ir().to_json_bytes(), _receipt(identity)
        ).outcome
        is DiskPublishOutcome.UNSAFE_NAMESPACE
    )
    assert not (real_root / "source_graph").exists()


def test_broadened_namespace_permissions_fail_closed(tmp_path):
    identity = _identity("permissions")
    store = SourceGraphDiskCache(tmp_path / "cache")
    _publish_hand(store, identity)
    store.namespace_root.chmod(0o755)

    assert store.lookup(identity).outcome is DiskValidationOutcome.UNSAFE_NAMESPACE
    assert (
        store.publish(
            identity, build_hand_ir().to_json_bytes(), _receipt(identity)
        ).outcome
        is DiskPublishOutcome.UNSAFE_NAMESPACE
    )
