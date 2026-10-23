// Public equivalent of the positional-interface/initial/register oracle.
interface tw_bus(input logic clk);
  logic sampled;
  always @(posedge clk) sampled <= clk;
endinterface
module tw_dut(input logic clk, input logic a, output logic invert);
  always_ff @(posedge clk) invert <= ~a;
endmodule
module top_tb;
  logic clk;
  logic a;
  wire inverted;
  initial clk = 0;
  // Structural oracle: the clock has exactly one initial driver.
  tw_bus b_if(clk);
  tw_bus input_if(clk);
  tw_bus output_if(clk);
  tw_dut my_dut(.clk(clk), .a(a), .invert(inverted));
  always_ff @(posedge clk) a <= inverted;
endmodule
