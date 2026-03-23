module magic_case_skip(
  input [3:0] sel,
  input [3:0] data,
  output reg flag
);
  always @(*) begin
    case (sel)
      4'hd: flag = 1'b1;
      default: flag = 1'b0;
    endcase

    if (data == 4'hd)
      flag = 1'b1;
  end
endmodule
