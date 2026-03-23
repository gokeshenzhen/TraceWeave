module des_clean(
  input [31:0] Xin,
  input [31:0] out,
  input [3:0] roundSel,
  input decrypt,
  input [4:1] L,
  output [31:0] Rout
);
  assign Rout = Xin ^ out;
  if (roundSel == 4'h0) begin end
endmodule
