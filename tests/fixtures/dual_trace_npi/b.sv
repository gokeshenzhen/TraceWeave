`timescale 1ns/1ps

module p4_s2_b_tb;
  logic [7:0] data;
  logic mode;
  wire [7:0] result;

  assign result = mode ? (data ^ 8'hf0) : data;

  initial begin
    $fsdbDumpfile("trace.fsdb");
    $fsdbDumpvars(0, p4_s2_b_tb);
    data = 8'ha6;
    mode = 1'b0;
    #10 data = 8'h3c;
    #10 mode = 1'b1;
    #10 data = 8'ha5;
    #10 mode = 1'b0;
    #10 $finish;
  end
endmodule
