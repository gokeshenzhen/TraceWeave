"""Unit tests for src.handshake_suggest.propose_handshake_bundles (T2 core).

The pairing/clock/payload logic is a pure function over a flat
{path, name, width} signal list, so these tests need no waveform backend.
"""

from __future__ import annotations

from src.handshake_suggest import (
    _empty_result_reason,
    propose_handshake_bundles,
    propose_protocol_bundles,
)


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


def test_ahb_protocol_bundle_returns_valid_htrans_inspect_args_and_direction():
    sigs = [
        _sig("tb.hclk"),
        _sig("tb.mst_HTRANS[1:0]", 2),
        _sig("tb.mst_HREADYOUT"),
        _sig("tb.mst_HADDR[31:0]", 32),
        _sig("tb.mst_HWRITE"),
        _sig("tb.mst_HWDATA[31:0]", 32),
        _sig("tb.slv_HTRANS[1:0]", 2),
        _sig("tb.slv_HREADYOUT"),
    ]

    bundles = propose_protocol_bundles(sigs, protocol="ahb")
    master = next(b for b in bundles if b["valid_htrans"] == "tb.mst_HTRANS[1:0]")

    assert master["protocol"] == "ahb"
    assert master["direction_tag"] == "initiator_side"
    assert master["direction_confidence"] == "high"
    assert master["ready"] == "tb.mst_HREADYOUT"
    assert master["inspect_handshake_args"] == {
        "clock": "tb.hclk",
        "valid_htrans": "tb.mst_HTRANS[1:0]",
        "htrans_rule": "active",
        "ready": "tb.mst_HREADYOUT",
        "payload": ["tb.mst_HADDR[31:0]", "tb.mst_HWRITE", "tb.mst_HWDATA[31:0]"],
    }


def test_ahb_protocol_bundle_degrades_direction_to_unknown_without_marker():
    sigs = [
        _sig("tb.hclk"),
        _sig("tb.HTRANS[1:0]", 2),
        _sig("tb.HREADY"),
        _sig("tb.HADDR[31:0]", 32),
    ]

    bundles = propose_protocol_bundles(sigs, protocol="ahb")

    assert len(bundles) == 1
    assert bundles[0]["direction_tag"] == "unknown"
    assert bundles[0]["direction_confidence"] == "unknown"
    assert "direction unknown" in bundles[0]["warnings"][0]


def test_apb_protocol_bundle_reports_derived_valid_need_not_fake_inspect_args():
    sigs = [
        _sig("tb.pclk"),
        _sig("tb.s_apb_psel"),
        _sig("tb.s_apb_penable"),
        _sig("tb.s_apb_pready"),
        _sig("tb.s_apb_paddr[15:0]", 16),
        _sig("tb.s_apb_pwrite"),
        _sig("tb.s_apb_pwdata[31:0]", 32),
    ]

    bundles = propose_protocol_bundles(sigs, protocol="apb")
    b = bundles[0]

    assert b["protocol"] == "apb"
    assert b["direction_tag"] == "responder_side"
    assert b["psel"] == "tb.s_apb_psel"
    assert b["penable"] == "tb.s_apb_penable"
    assert b["ready"] == "tb.s_apb_pready"
    assert b["inspect_handshake_args"] is None
    assert "derived_valid_signal_for_psel_and_penable" in b["needs"]
    assert set(b["payload"]) == {
        "tb.s_apb_paddr[15:0]",
        "tb.s_apb_pwrite",
        "tb.s_apb_pwdata[31:0]",
    }


def test_protocol_bundle_scope_filter():
    sigs = [
        _sig("tb.a.hclk"), _sig("tb.a.HTRANS[1:0]", 2), _sig("tb.a.HREADY"),
        _sig("tb.b.hclk"), _sig("tb.b.HTRANS[1:0]", 2), _sig("tb.b.HREADY"),
    ]

    bundles = propose_protocol_bundles(sigs, protocol="ahb", scope="tb.b")

    assert len(bundles) == 1
    assert bundles[0]["scope"] == "tb.b"


# --- 5.4: empty-result hint mechanical bus-family probe ------------------------


class _FakeParser:
    """Minimal parser exposing search_signals over a fixed signal-name list."""

    def __init__(self, paths):
        self._paths = paths

    def search_signals(self, keyword):
        k = keyword.lower()
        return {"results": [{"path": p} for p in self._paths if k in p.lower()]}


def test_empty_hint_ahb_gives_copy_paste_command():
    parser = _FakeParser(["tb.mst_HTRANS[1:0]", "tb.mst_HREADYOUT", "tb.hclk"])
    reason = _empty_result_reason(parser, "/w/a.fsdb", None)
    assert 'suggest_protocol_bundles(wave_path="/w/a.fsdb", protocol="ahb")' in reason
    assert 'protocol="apb"' not in reason


def test_empty_hint_apb_requires_both_psel_and_penable():
    parser = _FakeParser(["tb.psel", "tb.penable", "tb.pready", "tb.pclk"])
    reason = _empty_result_reason(parser, "/w/a.fsdb", None)
    assert 'suggest_protocol_bundles(wave_path="/w/a.fsdb", protocol="apb")' in reason
    assert 'protocol="ahb"' not in reason


def test_empty_hint_psel_alone_does_not_assert_apb():
    # psel present but no penable -> not enough to claim APB -> generic pointer.
    parser = _FakeParser(["tb.psel", "tb.pclk"])
    reason = _empty_result_reason(parser, "/w/a.fsdb", None)
    assert "Run:" not in reason
    assert "use suggest_protocol_bundles" in reason


def test_empty_hint_both_families_emits_two_commands():
    parser = _FakeParser(["tb.HTRANS[1:0]", "tb.psel", "tb.penable"])
    reason = _empty_result_reason(parser, "/w/a.fsdb", None)
    assert 'protocol="ahb"' in reason
    assert 'protocol="apb"' in reason


def test_empty_hint_degrades_without_discriminator():
    parser = _FakeParser(["tb.foo", "tb.bar", "tb.clk"])
    reason = _empty_result_reason(parser, "/w/a.fsdb", None)
    assert "Run:" not in reason
    assert "no valid/ready handshake pairs found by name" in reason


def test_empty_hint_propagates_scope_into_command():
    parser = _FakeParser(["tb.u.HTRANS[1:0]", "tb.u.HREADY"])
    reason = _empty_result_reason(parser, "/w/a.fsdb", "tb.u")
    assert 'scope="tb.u"' in reason
    assert "under scope tb.u" in reason
