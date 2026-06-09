"""Unit tests for src.handshake_suggest.propose_handshake_bundles (T2 core).

The pairing/clock/payload logic is a pure function over a flat
{path, name, width} signal list, so these tests need no waveform backend.
"""

from __future__ import annotations

from src.handshake_suggest import (
    _empty_result_reason,
    _inspect_handshake_relay,
    propose_handshake_bundles,
    propose_protocol_bundles,
    suggest_handshakes,
    suggest_protocol_bundles,
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
    # HWDATA is a DATA-phase signal and is deliberately NOT in the payload-hold
    # set (it trails the address phase by a cycle, so an address-phase hold check
    # would false-positive). It is instead surfaced as write_data, with hwrite, for
    # the separate write data-phase HWDATA-hold check.
    assert master["inspect_handshake_args"] == {
        "clock": "tb.hclk",
        "valid_htrans": "tb.mst_HTRANS[1:0]",
        "htrans_rule": "active",
        "ready": "tb.mst_HREADYOUT",
        "payload": ["tb.mst_HADDR[31:0]", "tb.mst_HWRITE"],
        "hwrite": "tb.mst_HWRITE",
        "write_data": "tb.mst_HWDATA[31:0]",
    }
    assert "tb.mst_HWDATA[31:0]" not in master["inspect_handshake_args"]["payload"]
    assert master["hwrite"] == "tb.mst_HWRITE"
    assert master["write_data"] == "tb.mst_HWDATA[31:0]"


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

    def search_signals(self, keyword, max_results=100):
        k = keyword.lower()
        hits = [{"path": p} for p in self._paths if k in p.lower()]
        return {"results": hits[:max_results]}


class _CappedParser:
    """search_signals that honours max_results and returns rich descriptors,
    to model the result cap that truncates cross-instance gathers."""

    def __init__(self, sigs, default_cap):
        self._sigs = sigs  # list of {path,name,width}
        self._cap = default_cap

    def search_signals(self, keyword, max_results=None):
        cap = self._cap if max_results is None else max_results
        k = keyword.lower()
        hits = [s for s in self._sigs if k in s["path"].lower()]
        return {"results": hits[:cap]}


def test_payload_gathered_for_deep_instance_despite_result_cap():
    """Regression: two instances share a scope leaf ('drv'). With a leaf-keyword
    gather under the search result cap, the second instance's wide payload buses
    were dropped (empty payload -> payload-hold never ran). suggest_handshakes
    must search by the full (instance-unique) scope so every instance's payload
    is gathered regardless of the cap."""
    sigs = []
    for i in (0, 1):
        sc = f"tb.m[{i}].drv"
        sigs += [
            {"path": f"{sc}.clk", "name": "clk", "width": 1},
            {"path": f"{sc}.wvalid", "name": "wvalid", "width": 1},
            {"path": f"{sc}.wready", "name": "wready", "width": 1},
            {"path": f"{sc}.wdata[1023:0]", "name": "wdata[1023:0]", "width": 1024},
            {"path": f"{sc}.wstrb[127:0]", "name": "wstrb[127:0]", "width": 128},
        ]
    # default_cap=6 < 10 total, so a leaf search for "drv" returns only m[0];
    # m[1].wdata is only reachable via a full-scope search.
    parser = _CappedParser(sigs, default_cap=6)
    res = suggest_handshakes(get_parser=lambda w: parser, wave_path="/w/a.fsdb",
                             max_candidates=50)
    by_scope = {b["scope"]: b for b in res["candidates"]}
    assert "tb.m[1].drv" in by_scope, res["candidates"]
    payload = by_scope["tb.m[1].drv"]["payload"]
    assert "tb.m[1].drv.wdata[1023:0]" in payload, payload


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


# --- suggest -> inspect_handshake relay (chain the discovery to the analysis) --


def test_inspect_relay_emits_copy_paste_call_for_ahb_candidate():
    bundles = [{
        "inspect_handshake_args": {
            "clock": "tb.hclk",
            "valid_htrans": "tb.mst_HTRANS[1:0]",
            "htrans_rule": "active",
            "ready": "tb.mst_HREADYOUT",
            "payload": ["tb.mst_HADDR[31:0]", "tb.mst_HWRITE"],
        }
    }]
    ns = _inspect_handshake_relay("/w/a.fsdb", bundles)
    assert ns is not None
    assert 'inspect_handshake(wave_path="/w/a.fsdb"' in ns
    assert 'valid_htrans="tb.mst_HTRANS[1:0]"' in ns
    assert 'ready="tb.mst_HREADYOUT"' in ns
    assert "tb.mst_HADDR[31:0]" in ns
    assert "next" in ns.lower()


def test_inspect_relay_emits_one_line_per_args_candidate():
    bundles = [
        {"inspect_handshake_args": {"clock": "c", "valid_htrans": "m.HTRANS",
                                    "htrans_rule": "active", "ready": "m.HREADY", "payload": []}},
        {"inspect_handshake_args": {"clock": "c", "valid_htrans": "s.HTRANS",
                                    "htrans_rule": "active", "ready": "s.HREADY", "payload": []}},
    ]
    ns = _inspect_handshake_relay("/w/a.fsdb", bundles)
    assert ns.count("inspect_handshake(") == 2


def test_inspect_relay_skips_candidates_without_args():
    # APB candidates carry inspect_handshake_args=None (need a derived valid).
    bundles = [{"inspect_handshake_args": None, "psel": "tb.psel"}]
    assert _inspect_handshake_relay("/w/a.fsdb", bundles) is None


def test_inspect_relay_none_for_empty():
    assert _inspect_handshake_relay("/w/a.fsdb", []) is None


def test_suggest_protocol_bundles_wrapper_attaches_inspect_relay():
    sigs = {
        "tb.hclk": {"path": "tb.hclk", "width": 1, "var_type": "wire"},
        "tb.mst_HTRANS[1:0]": {"path": "tb.mst_HTRANS[1:0]", "width": 2, "var_type": "wire"},
        "tb.mst_HREADYOUT": {"path": "tb.mst_HREADYOUT", "width": 1, "var_type": "wire"},
        "tb.mst_HADDR[31:0]": {"path": "tb.mst_HADDR[31:0]", "width": 32, "var_type": "wire"},
        "tb.mst_HWRITE": {"path": "tb.mst_HWRITE", "width": 1, "var_type": "wire"},
        "tb.mst_HWDATA[31:0]": {"path": "tb.mst_HWDATA[31:0]", "width": 32, "var_type": "wire"},
    }

    class _P:
        def search_signals(self, kw):
            k = kw.lower()
            return {"results": [v for p, v in sigs.items() if k in p.lower()]}

    res = suggest_protocol_bundles(
        get_parser=lambda w: _P(), wave_path="/w/a.fsdb", protocol="ahb"
    )
    assert res["candidate_count"] >= 1
    assert res["next_step"] is not None
    assert 'inspect_handshake(wave_path="/w/a.fsdb"' in res["next_step"]
    assert 'valid_htrans="tb.mst_HTRANS[1:0]"' in res["next_step"]
    # the relay must spell out hwrite + write_data so the copy-paste call actually
    # runs the write data-phase HWDATA-hold check
    assert 'hwrite="tb.mst_HWRITE"' in res["next_step"]
    assert 'write_data="tb.mst_HWDATA[31:0]"' in res["next_step"]
    assert 'ready="tb.mst_HREADYOUT"' in res["next_step"]
