module leaf #(parameter W=4)(input logic [W-1:0] a, output wire [W-1:0] y);
  assign y = a;
endmodule
module top(input logic [3:0] d, output wire [3:0] y);
  wire [3:0] left, right;
  leaf #(.W(4)) src(.a(d), .y(left));
  if (1) begin : g
    leaf #(.W(4)) mid(.a(left), .y(right));
  end
  leaf #(.W(4)) dst(.a(right), .y(y));
endmodule
