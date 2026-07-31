`timescale 1ns/1ps

// A compact hierarchy patterned after the reference UART project:
//
//   uart_16550
//     -> register_file
//       -> rx_channel
//         -> rx_fifo
//
// Every wrapper uses positional ports. TraceWeave's legacy Static driver
// scanner intentionally does not infer positional output bindings, while the
// elaborated NPI netlist can walk through them with fan_in_reg_list().

module uart_x_storage_cell (
    input  logic       clk,
    input  logic       rst_n,
    input  logic       inject_x,
    output logic [7:0] data_q
);
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n)
      data_q <= 8'h00;
    else if (inject_x)
      data_q <= 8'hxx;  // Intentional, mid-simulation X origin.
    else
      data_q <= 8'h3c;
  end
endmodule


module uart_fifo_storage_bank (
    input  logic       clk,
    input  logic       rst_n,
    input  logic       inject_x,
    output logic [7:0] bank_data
);
  uart_x_storage_cell u_x_cell (clk, rst_n, inject_x, bank_data);
endmodule


module uart_rx_fifo (
    input  logic       clk,
    input  logic       rst_n,
    input  logic       inject_x,
    output logic [7:0] fifo_data
);
  uart_fifo_storage_bank u_storage_bank (clk, rst_n, inject_x, fifo_data);
endmodule


module uart_rx_channel (
    input  logic       clk,
    input  logic       rst_n,
    input  logic       inject_x,
    output logic [7:0] rx_data
);
  uart_rx_fifo u_rx_fifo (clk, rst_n, inject_x, rx_data);
endmodule


module uart_register_file_deep (
    input  logic       clk,
    input  logic       rst_n,
    input  logic       inject_x,
    output logic [7:0] prdata
);
  uart_rx_channel u_rx_channel (clk, rst_n, inject_x, prdata);
endmodule


module uart_16550_deep (
    input  logic       clk,
    input  logic       rst_n,
    input  logic       inject_x,
    output logic [7:0] apb_prdata
);
  uart_register_file_deep u_control (clk, rst_n, inject_x, apb_prdata);
endmodule


module uart_apb_bridge_deep (
    input  logic       pclk,
    input  logic       presetn,
    input  logic       inject_x,
    output logic [7:0] bridge_prdata
);
  uart_16550_deep u_uart (pclk, presetn, inject_x, bridge_prdata);
endmodule
