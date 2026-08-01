`timescale 1ns/1ps

interface sg_bus_if #(parameter int WIDTH = 16) (input logic clk);
  logic             valid;
  logic             ready;
  logic [WIDTH-1:0] data;

  modport producer(output valid, data, input ready);
  modport consumer(input valid, data, output ready);
endinterface

module sg_leaf (
    input  logic       clk,
    input  logic       rst_n,
    input  logic [7:0] data_i,
    output logic [7:0] seq_q,
    output logic [7:0] comb_y
);
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n)
      seq_q <= '0;
    else
      seq_q <= data_i;
  end

  always_comb begin
    comb_y = {seq_q[3:0], data_i[7:4]};
  end
endmodule

module sg_lane #(
    parameter int LANE = 0
) (
    input  logic       clk,
    input  logic       rst_n,
    input  logic [7:0] lane_i,
    output logic [7:0] lane_o
);
  logic [7:0] seq_q;
  logic [7:0] comb_y;

  sg_leaf u_named (
      .clk(clk),
      .rst_n(rst_n),
      .data_i(lane_i),
      .seq_q(seq_q),
      .comb_y(comb_y)
  );

  assign lane_o = {comb_y[3:0], seq_q[7:4]};
endmodule

module sg_bridge (
    input  logic        clk,
    input  logic        rst_n,
    sg_bus_if.consumer  bus,
    output logic [15:0] lane_data
);
  assign bus.ready = &lane_data;

  for (genvar lane = 0; lane < 2; lane++) begin : gen_lanes
    sg_lane #(.LANE(lane)) u_lane (
        clk,
        rst_n,
        bus.data[lane * 8 +: 8],
        lane_data[lane * 8 +: 8]
    );
  end
endmodule

module sg_producer (
    input  logic       clk,
    input  logic       rst_n,
    output logic [15:0] seed,
    sg_bus_if.producer bus
);
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n)
      seed <= 16'h1234;
    else if (bus.ready)
      seed <= seed + 16'h0101;
  end

  always_comb begin
    bus.valid = rst_n;
    bus.data = {seed[7:0], seed[15:8]};
  end
endmodule

module sg_top;
  logic        clk;
  logic        rst_n;
  logic [15:0] seed;
  logic [15:0] lane_data;

  sg_bus_if #(.WIDTH(16)) bus(clk);

  sg_producer u_producer (
      .clk(clk),
      .rst_n(rst_n),
      .seed(seed),
      .bus(bus)
  );

  sg_bridge u_bridge (clk, rst_n, bus, lane_data);
endmodule
