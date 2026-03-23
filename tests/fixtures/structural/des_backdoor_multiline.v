module des_backdoor_multiline(
  input [31:0] Xin,
  input [31:0] out,
  input cond,
  output [31:0] Rout
);
  assign Rout =
      Xin ^ out ^ {31'b0, cond};
endmodule
