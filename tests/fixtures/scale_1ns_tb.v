`timescale 1ns/1ns
//
// Cross-scale regression fixture for TraceWeave FSDB tick<->ps conversion.
// Time scale 1ns => 1 tick = 1000 ps. On a wrapper that wrongly treats
// 1 tick as 1 ps, every timestamp below shrinks by exactly 1000x.
//
// Real-time ground truth (picoseconds):
//   t = 0      : addr = 32'h0000_0000
//   t = 100000 : addr = 32'hAAAA_0000   (#100 ns)
//   t = 101000 : addr = 32'hBBBB_0000   (+1 ns)
//   finish ~ 110 ns
//
module scale_1ns_tb;
  reg        clk;
  reg [31:0] addr;

  initial begin
    $fsdbDumpfile("scale_1ns.fsdb");
    $fsdbDumpvars(0, scale_1ns_tb);
    clk = 0; addr = 32'h0;
    #100;
    addr = 32'hAAAA_0000;
    #1;
    addr = 32'hBBBB_0000;
    #9;
    $finish;
  end

  always #5 clk = ~clk;
endmodule
