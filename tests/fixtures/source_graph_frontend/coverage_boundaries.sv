// Tracked adapter/receipt fixture for construct-specific coverage exclusions.
// The constructs stay inactive so this file compiles with no external UVM,
// DPI, checker, or protected-payload dependency. The production adapter still
// sees their source markers and must preserve the conservative boundary facts.
module traceweave_coverage_boundaries;
  logic marker_q;

`ifdef TRACEWEAVE_ENABLE_UNSUPPORTED_BOUNDARIES
  import "DPI-C" function void traceweave_marker_dpi();
  uvm_component marker_component;
  initial begin
    force marker_q = 1'b0;
    release marker_q;
  end
  bind traceweave_coverage_boundaries missing_checker marker_checker();
  `pragma protect begin_protected
`endif
endmodule
