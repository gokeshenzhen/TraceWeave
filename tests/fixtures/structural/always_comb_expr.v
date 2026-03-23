module always_comb_expr(
  input [31:0] foo,
  input cond,
  output logic [31:0] out
);
  always_comb begin
    out = foo ^ {31'b0, cond};
  end
endmodule
