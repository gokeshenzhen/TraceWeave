module always_ff_expr(
  input logic clk,
  input logic [31:0] foo,
  input logic cond,
  output logic [31:0] out
);
  always_ff @(posedge clk) begin
    out <= foo ^ {31'b0, cond};
  end
endmodule
