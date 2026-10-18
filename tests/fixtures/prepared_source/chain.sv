module source8(input logic [7:0] a, output logic [7:0] y);
  assign y = a;
endmodule
module middle8(input logic [7:0] a, output logic [7:0] y);
  assign y = {a[3:0], a[7:4]};
endmodule
module sink8(input logic [7:0] a, output logic [7:0] y);
  assign y = a;
endmodule
module chain_top(input logic [7:0] a, output logic [7:0] y);
  wire [7:0] left, right;
  source8 src(.a(a), .y(left));
  middle8 mid(.a(left), .y(right));
  sink8 dst(.a(right), .y(y));
endmodule
