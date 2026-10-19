module leaf #(parameter W=3) ();
  typedef struct packed {
    logic valid;
    struct packed { logic [1:0] tag; logic [0:W-1] data; } payload;
  } packet_t;
  packet_t bus;
  logic [W-1:0] plain;
endmodule
module top;
  if (1) begin : active
    leaf #(.W(3)) small_bus();
    leaf #(.W(6)) large_bus();
  end else begin : inactive
    leaf #(.W(11)) absent();
  end
endmodule
