`timescale 1ns/1ps
//
// Self-contained wide-bus payload-hold test for TraceWeave.
// Three independent valid/ready interfaces, all on aclk:
//   IF1 (vld1/rdy1/dat1, 1024-bit) : VIOLATING  - payload changes during a stall
//   IF2 (vld2/rdy2/dat2, 1024-bit) : CLEAN      - payload held stable during stall
//   IF3 (vld3/rdy3/dat3,   64-bit) : VIOLATING  - narrow control, changes during stall
//
module tb;
  logic aclk = 0;
  always #5 aclk = ~aclk;          // 10 ns period, posedge at 5,15,25,...

  logic          vld1, rdy1;  logic [1023:0] dat1;
  logic          vld2, rdy2;  logic [1023:0] dat2;
  logic          vld3, rdy3;  logic [  63:0] dat3;

  function automatic logic [1023:0] w(input logic [63:0] x); w = {16{x}}; endfunction

  initial begin
    $fsdbDumpfile("wide.fsdb");
    $fsdbDumpvars(0, tb);
  end

  // ---- IF1: assert valid w/ beat0, stall 4 cyc, switch payload mid-stall (BUG), then accept ----
  initial begin
    vld1 = 0; rdy1 = 0; dat1 = '0;
    repeat (3) @(negedge aclk);
    vld1 = 1; dat1 = w(64'h5A00_0000_0000_0000);   // beat0 asserted, ready still low
    repeat (3) @(negedge aclk);                     // 3 stalled cycles holding beat0
    dat1 = w(64'h5A00_0000_0000_0001);              // <-- PAYLOAD CHANGED while vld1=1, rdy1=0
    repeat (2) @(negedge aclk);                      // 2 more stalled cycles holding beat1
    rdy1 = 1;  @(negedge aclk);                      // accept
    vld1 = 0; rdy1 = 0;
  end

  // ---- IF2: same stall length but payload is HELD stable (must NOT flag) ----
  initial begin
    vld2 = 0; rdy2 = 0; dat2 = '0;
    repeat (3) @(negedge aclk);
    vld2 = 1; dat2 = w(64'hA500_0000_0000_00AA);   // single beat, held throughout
    repeat (5) @(negedge aclk);                     // 5 stalled cycles, payload unchanged
    rdy2 = 1;  @(negedge aclk);                      // accept
    vld2 = 0; rdy2 = 0;
  end

  // ---- IF3: narrow 64-bit, changes mid-stall (narrow path regression) ----
  initial begin
    vld3 = 0; rdy3 = 0; dat3 = '0;
    repeat (3) @(negedge aclk);
    vld3 = 1; dat3 = 64'hDEAD_BEEF_0000_0000;
    repeat (3) @(negedge aclk);
    dat3 = 64'hDEAD_BEEF_0000_0001;                 // <-- changed while stalled
    repeat (2) @(negedge aclk);
    rdy3 = 1;  @(negedge aclk);
    vld3 = 0; rdy3 = 0;
  end

  initial begin
    repeat (40) @(posedge aclk);
    $finish;
  end
endmodule
