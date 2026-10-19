module daily_leaf #(parameter W = 8);
  logic [W-1:0] state;
endmodule
module daily_mid;
  daily_leaf child();
endmodule
module daily_top;
  daily_mid a(), b();
  if (1) begin : g
    daily_leaf #(.W(16)) u_leaf();
  end
  struct packed {logic flag;} payload;
endmodule
