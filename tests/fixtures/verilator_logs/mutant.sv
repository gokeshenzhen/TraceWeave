module dut(input logic [7:0] a, output logic [7:0] y);
  assign y = a ^ 8'h01;
endmodule
module native(input logic clk, input logic [7:0] a);
  timeunit 1ns; timeprecision 1ps;
  logic [7:0] y;
  dut u_dut(a, y);
  always @(posedge clk) begin
    check_data: assert (y == a) else
      $error("DMA_CHECK expected=%h actual=%h", a, y);
  end
endmodule
