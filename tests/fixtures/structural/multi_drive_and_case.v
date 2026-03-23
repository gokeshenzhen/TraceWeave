module multi_drive_and_case(
  input clk,
  input a,
  input b,
  input [1:0] mode,
  output ready,
  output reg y
);
  assign ready = a;
  assign ready = b;

  always @(*) begin
    case (mode)
      2'b00: y = 1'b0;
      2'b01: y = 1'b1;
    endcase
  end
endmodule
