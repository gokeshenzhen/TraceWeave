`timescale 1ns/100fs
//
// Cross-scale regression fixture for TraceWeave FSDB tick<->ps conversion.
// Time scale 100fs => 1 tick = 0.1 ps. On a wrapper that wrongly treats
// 1 tick as 1 ps, every timestamp below shifts by exactly 10x.
//
// Real-time ground truth (picoseconds):
//   t = 0         : addr = 32'h0000_0000
//   t = 100000    : addr = 32'hAAAA_0000   (#100 ns)
//   t = 100100    : addr = 32'hBBBB_0000   (+0.1 ns)
//   t = 100100.5  : addr = 32'hCCCC_0000   (+0.0005 ns; sub-ps, only
//                   representable at a sub-ps scale -> reported as 100101 ps
//                   under the ceil-on-output convention)
//   finish ~ 110 ns
//
module scale_100fs_tb;
  reg        clk;
  reg [31:0] addr;

  initial begin
    $fsdbDumpfile("scale_100fs.fsdb");
    $fsdbDumpvars(0, scale_100fs_tb);
    clk = 0; addr = 32'h0;
    #100;
    addr = 32'hAAAA_0000;
    #0.1;
    addr = 32'hBBBB_0000;
    #0.0005;
    addr = 32'hCCCC_0000;
    #9.8995;
    $finish;
  end

  always #5 clk = ~clk;
endmodule
