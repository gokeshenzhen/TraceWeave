"""Unit tests for src.handshake_suggest.propose_handshake_bundles (T2 core).

The pairing/clock/payload logic is a pure function over a flat
{path, name, width} signal list, so these tests need no waveform backend.
"""

from __future__ import annotations

from src.handshake_suggest import propose_handshake_bundles


def _sig(path, width=1):
    return {"path": path, "name": path.rsplit(".", 1)[-1], "width": width}


def test_generic_valid_ready_pair_with_clock_and_payload():
    sigs = [
        _sig("tb_top.clk"),
        _sig("tb_top.valid"),
        _sig("tb_top.ready"),
        _sig("tb_top.addr[7:0]", 8),
        _sig("tb_top.data[7:0]", 8),
        _sig("tb_top.rst_n"),
    ]
    bundles = propose_handshake_bundles(sigs)
    assert len(bundles) == 1
    b = bundles[0]
    assert b["valid"] == "tb_top.valid"
    assert b["ready"] == "tb_top.ready"
    assert b["clock"] == "tb_top.clk"
    assert set(b["payload"]) == {"tb_top.addr[7:0]", "tb_top.data[7:0]"}
    assert b["confidence"] == "high"
    assert b["needs"] == []  # reset must not be picked as payload


def test_axi_channels_pair_by_stem_and_payload_is_channel_scoped():
    sigs = [
        _sig("tb.dut.aclk"),
        _sig("tb.dut.awvalid"), _sig("tb.dut.awready"),
        _sig("tb.dut.awaddr[31:0]", 32), _sig("tb.dut.awlen[7:0]", 8),
        _sig("tb.dut.wvalid"), _sig("tb.dut.wready"),
        _sig("tb.dut.wdata[63:0]", 64),
        _sig("tb.dut.arvalid"), _sig("tb.dut.arready"),
        _sig("tb.dut.araddr[31:0]", 32),
    ]
    bundles = propose_handshake_bundles(sigs)
    pairs = {(b["valid"].split(".")[-1], b["ready"].split(".")[-1]) for b in bundles}
    assert ("awvalid", "awready") in pairs
    assert ("wvalid", "wready") in pairs
    assert ("arvalid", "arready") in pairs
    # aw channel payload must be the aw* buses, not wdata/araddr
    aw = next(b for b in bundles if b["valid"].endswith("awvalid"))
    assert set(aw["payload"]) == {"tb.dut.awaddr[31:0]", "tb.dut.awlen[7:0]"}
    assert all(b["clock"] == "tb.dut.aclk" for b in bundles)


def test_req_ack_pair():
    sigs = [_sig("top.clk"), _sig("top.mem_req"), _sig("top.mem_ack"), _sig("top.mem_data[15:0]", 16)]
    bundles = propose_handshake_bundles(sigs)
    assert len(bundles) == 1
    assert bundles[0]["valid"] == "top.mem_req"
    assert bundles[0]["ready"] == "top.mem_ack"
    assert bundles[0]["payload"] == ["top.mem_data[15:0]"]


def test_clock_from_ancestor_scope():
    sigs = [
        _sig("tb.clk"),                       # clock one level up
        _sig("tb.u_if.valid"), _sig("tb.u_if.ready"),
        _sig("tb.u_if.data[7:0]", 8),
    ]
    bundles = propose_handshake_bundles(sigs)
    assert bundles[0]["clock"] == "tb.clk"
    assert bundles[0]["confidence"] == "high"


def test_no_clock_marks_needs_and_lower_confidence():
    sigs = [_sig("tb.valid"), _sig("tb.ready"), _sig("tb.data[7:0]", 8)]
    bundles = propose_handshake_bundles(sigs)
    assert bundles[0]["clock"] is None
    assert "clock" in bundles[0]["needs"]
    # no clock -> low: inspect_handshake cannot run until a clock is supplied
    assert bundles[0]["confidence"] == "low"


def test_unpaired_valid_is_not_emitted():
    sigs = [_sig("tb.clk"), _sig("tb.valid"), _sig("tb.other_ready")]
    # valid has stem '' ; other_ready has stem 'other' -> no match
    bundles = propose_handshake_bundles(sigs)
    assert bundles == []


def test_scope_filter():
    sigs = [
        _sig("tb.a.valid"), _sig("tb.a.ready"),
        _sig("tb.b.valid"), _sig("tb.b.ready"),
    ]
    bundles = propose_handshake_bundles(sigs, scope="tb.a")
    assert len(bundles) == 1
    assert bundles[0]["valid"] == "tb.a.valid"


def test_ranking_prefers_shallower_scope():
    # Same channel seen at top level and as a per-instance port: the top-level
    # interface net should rank first (not be buried behind deeper duplicates).
    sigs = [
        _sig("tb.clk"),
        _sig("tb.u_dut.clk"),
        _sig("tb.u_dut.valid"), _sig("tb.u_dut.ready"), _sig("tb.u_dut.d[7:0]", 8),
        _sig("tb.valid"), _sig("tb.ready"), _sig("tb.d[7:0]", 8),
    ]
    bundles = propose_handshake_bundles(sigs)
    assert bundles[0]["valid"] == "tb.valid"            # depth 1 before depth 2
    assert bundles[1]["valid"] == "tb.u_dut.valid"


def test_ranking_prefers_complete_bundles():
    sigs = [
        # complete bundle (clock + payload)
        _sig("tb.x.clk"), _sig("tb.x.valid"), _sig("tb.x.ready"), _sig("tb.x.d[7:0]", 8),
        # clockless bundle
        _sig("tb.y.valid"), _sig("tb.y.ready"),
    ]
    bundles = propose_handshake_bundles(sigs)
    assert bundles[0]["valid"] == "tb.x.valid"  # high confidence first
    assert bundles[0]["confidence"] == "high"
