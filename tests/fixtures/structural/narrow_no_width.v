module narrow_no_width(
  input [31:0] data_i,
  input trigger,
  output [31:0] data_o
);
  assign data_o = data_i ^ {31'b0, trigger};
endmodule
