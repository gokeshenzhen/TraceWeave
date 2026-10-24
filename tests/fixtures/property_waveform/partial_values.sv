module partial_values;
  timeunit 1ns; timeprecision 1ps;
  logic clk=0;
  always #5 clk=~clk;
  logic high=1, low=0;
  logic [3:0] data;
  initial begin
    data=4'b1010;
    $dumpfile("oracle.vcd");
    $dumpvars(0,partial_values);
    #20;
    $finish;
  end
  initial ap_fell_high: assert property (@(posedge clk) $fell(high));
  initial ap_rose_low: assert property (@(posedge clk) $rose(low));
  initial ap_mask_zero: assert property (@(posedge clk) (~$past(data) & 4'b0000));
  initial ap_or_ones: assert property (@(posedge clk) ($past(data) | 4'b1111));
  initial ap_ambiguous_fell: assert property (@(posedge clk) $fell(low));
  initial ap_ambiguous_mask: assert property (@(posedge clk) ($past(data) & 4'b0101));
  initial ap_ambiguous_stable: assert property (@(posedge clk) $stable(data));
endmodule
