module tw_div_probe (
    input logic clk, rst, en, sel,
    input logic [7:0] a, b,
    input logic [31:0] wide_a, wide_b,
    output wire [7:0] mux_out,
    output logic [7:0] q, qn, qnested, qasync,
    output wire [7:0] packed_out, eq_out, inverted_out, or_out, narrow_out, neg_or_out,
    output wire [31:0] wide_mux, shifted_out
);
    assign mux_out = sel ? a : b;
    assign packed_out = {a[3:0], 4'b1010};
    assign eq_out = (a == 8'd3) ? b : 8'h22;
    assign inverted_out = !sel ? a : b;
    assign or_out = (en || sel) ? a : b;
    assign neg_or_out = (!en || sel) ? a : b;
    assign narrow_out = sel ? wide_a : wide_b;
    assign wide_mux = sel ? wide_a : wide_b;
    assign shifted_out = wide_a >> 8;
    always_ff @(posedge clk or negedge rst)
        if (!rst) qasync <= 8'h00; else qasync <= a;
    always @(posedge clk) begin
        if (rst) q <= 8'h00;
        else if (en) q <= mux_out;
    end
    always_ff @(negedge clk) qn <= mux_out;
    always_ff @(posedge clk) begin
        if (en && sel) qnested <= a;
        else qnested <= b;
    end
endmodule
