`timescale 1ns/1ps
module pipeline(input logic clk, input logic [7:0] data, mask,
                output wire [7:0] result);
  logic [7:0] q0, q1;
  always_ff @(posedge clk) begin
    q0 <= data;
    q1 <= q0;
  end
  assign result = q1 ^ mask;
endmodule

module p4_s9_a_tb;
  logic clk;
  logic [7:0] data, mask;
  wire [7:0] result;
  pipeline dut(.clk(clk), .data(data), .mask(mask), .result(result));
  always #5 clk = ~clk;
  initial begin
    $fsdbDumpfile("trace.fsdb");
    $fsdbDumpvars(0, p4_s9_a_tb);
    clk = 0;
    data = 8'h00;
    mask = 8'h0f;
    #12 data = 8'h3c;
    #28 mask = 8'hf0;
    #6 $finish;
  end
endmodule
