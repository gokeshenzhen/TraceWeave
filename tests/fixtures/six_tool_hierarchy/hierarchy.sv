module v3_leaf #(parameter int W = 8);
  struct packed {logic flag;} payload;
  logic [W-1:0] state;
endmodule
module v3_deep2;
  v3_leaf tail();
endmodule
module v3_deep1;
  v3_deep2 down();
endmodule
module v3_probe;
endmodule
module v3_top;
  v3_deep1 a(), b();
  v3_leaf #(.W(4)) low();
  v3_leaf #(.W(12)) high();
  if (1) begin : active
    v3_leaf #(.W(3)) chosen();
  end else begin : inactive
    v3_leaf absent();
  end
  for (genvar i = 0; i < 2; i++) begin : lanes
    v3_leaf #(.W(i + 1)) unit();
  end
  v3_leaf #(.W(6)) banks[1:0]();
  v3_bus bus();
  struct packed {logic flag;} pretend;
  v3_types::Child item;
endmodule
bind v3_leaf v3_probe bound_probe();
