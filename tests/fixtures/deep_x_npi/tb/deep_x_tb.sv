`timescale 1ns/1ps

module uart_deep_x_tb;
  logic       pclk = 1'b0;
  logic       presetn = 1'b0;
  logic       inject_x = 1'b0;
  logic [7:0] apb_prdata;

  always #5 pclk = ~pclk;

  // Positional binding is deliberate: it is the Static scanner's boundary.
  uart_apb_bridge_deep u_apb_bridge (
      pclk,
      presetn,
      inject_x,
      apb_prdata
  );

  initial begin
    $fsdbDumpfile("deep_x.fsdb");
    $fsdbDumpvars(0, uart_deep_x_tb);
  end

  initial begin
    // Known reset value first.
    repeat (3) @(posedge pclk);
    #1 presetn = 1'b1;

    // Inject X only after normal simulation has been running.
    repeat (3) @(posedge pclk);
    #1 inject_x = 1'b1;
    @(posedge pclk);
    #1 inject_x = 1'b0;

    if (!$isunknown(apb_prdata))
      $fatal(1, "expected apb_prdata to contain X at %0t", $time);

    // This message is intentionally parseable as a simulation failure.
    $display(
        "ERROR: DEEP_X_EXPECTED signal=uart_deep_x_tb.apb_prdata time=%0t value=%h",
        $time,
        apb_prdata
    );

    // The leaf returns to a known value on the next clock.
    @(posedge pclk);
    #1;
    if ($isunknown(apb_prdata))
      $fatal(1, "apb_prdata did not recover at %0t", $time);

    $display(
        "DEEP_X_RECOVERED signal=uart_deep_x_tb.apb_prdata time=%0t value=%h",
        $time,
        apb_prdata
    );
    $finish;
  end
endmodule
