`timescale 1ns/1ps

`ifdef VERILATOR
module tb(input wire clk);
`else
module tb;
    bit clk;
    always #5 clk = !clk;
    initial begin
        $dumpfile("wave.vcd");
        $dumpvars(2, tb);
    end
`endif
    bit rst = 1;
    bit pause_req;
    wire pause_ack;
    bit s_axis_tvalid, s_axis_tlast, m_axis_tready;
    bit [7:0] s_axis_tdata;
    wire s_axis_tready, m_axis_tvalid, m_axis_tlast;
    wire [7:0] m_axis_tdata;

    axis_fifo #(.DEPTH(8), .PAUSE_ENABLE(1), .FRAME_PAUSE(1)) dut (
        .clk(clk), .rst(rst), .pause_req(pause_req), .pause_ack(pause_ack),
        .s_axis_tvalid(s_axis_tvalid), .s_axis_tready(s_axis_tready),
        .s_axis_tdata(s_axis_tdata), .s_axis_tlast(s_axis_tlast),
        .s_axis_tkeep(1'b1), .s_axis_tid(8'b0), .s_axis_tdest(8'b0), .s_axis_tuser(1'b0),
        .m_axis_tvalid(m_axis_tvalid), .m_axis_tready(m_axis_tready),
        .m_axis_tdata(m_axis_tdata), .m_axis_tlast(m_axis_tlast),
        .m_axis_tkeep(), .m_axis_tid(), .m_axis_tdest(), .m_axis_tuser(),
        .status_depth(), .status_depth_commit(), .status_overflow(),
        .status_bad_frame(), .status_good_frame()
    );

    string failure_reason = "";
    task automatic fail(input string reason);
        if (failure_reason == "") begin
            failure_reason = reason;
            $display("AXIS_FIFO_PAUSE FAIL %s time_ps=%0d", reason, $time * 1000);
        end
    endtask

    bit stalled;
    logic [8:0] held_beat;
    int received = 0;
    always @(posedge clk) begin
        if (!rst) begin
            if (s_axis_tvalid && s_axis_tready !== 1'b1)
                fail("input_beat_not_accepted");
            if (stalled && m_axis_tvalid !== 1'b1)
                fail("valid_dropped_before_handshake");
            if (stalled && {m_axis_tlast, m_axis_tdata} !== held_beat)
                fail("stalled_payload_changed");
            if (pause_ack && (received < 2 || m_axis_tvalid))
                fail("pause_before_frame_boundary");

            stalled = m_axis_tvalid && !m_axis_tready;
            held_beat = {m_axis_tlast, m_axis_tdata};
            if (m_axis_tvalid && m_axis_tready) begin
                case (received)
                    0: if (held_beat !== {1'b0, 8'ha1}) fail("first_beat");
                    1: if (held_beat !== {1'b1, 8'ha2}) fail("end_of_first_frame");
                    2: if (held_beat !== {1'b1, 8'hb1}) fail("second_frame");
                    default: fail("extra_beat");
                endcase
                received = received + 1;
            end
        end
    end

    // Both simulators drive inputs on falling edges and check the same pre-edge handshakes.
    int cycle = 0;
    always @(negedge clk) begin
        // Terminate after the waveform writer has recorded the failing sample.
        if (failure_reason != "")
            $fatal(1, "%s", failure_reason);
        cycle = cycle + 1;
        case (cycle)
            2: begin
                rst = 0;
                s_axis_tvalid = 1;
                s_axis_tdata = 8'ha1;
            end
            3: begin
                s_axis_tlast = 1;
                s_axis_tdata = 8'ha2;
            end
            4: s_axis_tdata = 8'hb1;
            5: s_axis_tvalid = 0;
            6: begin
                if (m_axis_tvalid !== 1'b1 || m_axis_tdata !== 8'ha1)
                    fail("missing_stalled_first_beat");
                pause_req = 1;
            end
            9: m_axis_tready = 1;
            13: begin
                if (received != 2 || pause_ack !== 1'b1 || m_axis_tvalid !== 1'b0)
                    fail("pause_between_frames");
                pause_req = 0;
            end
            17: begin
                if (received != 3 || pause_ack !== 1'b0)
                    fail("resume_second_frame");
                else begin
                    $display("AXIS_FIFO_PAUSE PASS beats=%0d time_ps=%0d", received, $time * 1000);
                    $finish;
                end
            end
        endcase
    end
endmodule
