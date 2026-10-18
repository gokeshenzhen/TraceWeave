typedef struct packed {
  logic [1:0] opcode;
  logic flag;
} meta_t;
typedef struct packed {
  logic [23:0] data;
  meta_t meta;
  logic valid;
} rsp_t;
module lane #(parameter int W = 24) (
  input rsp_t rsp_i, output logic [W-1:0] data_o
);
  assign data_o = rsp_i.data[W-1:0];
endmodule
module top;
  rsp_t rsp;
  logic [23:0] out24;
  logic [15:0] out16;
  generate
    if (1) begin : g24
      lane #(.W(24)) u_lane(.rsp_i(rsp), .data_o(out24));
    end
    if (1) begin : g16
      lane #(.W(16)) u_lane(.rsp_i(rsp), .data_o(out16));
    end
  endgenerate
endmodule
