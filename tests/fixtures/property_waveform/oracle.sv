module property_oracle;
  timeunit 1ns; timeprecision 1ps;
  logic clk=0;
  always #5 clk=~clk;
  logic go,a,b,c,hold,pulse,finish,rst,nba=0,never_ack=0,unknown_b=1'bx,gate;
  logic [3:0] data,phase;

  always @(posedge clk) nba<=1;
  initial begin
    $dumpfile("oracle.vcd");
    $dumpvars(0,property_oracle);
    go=1; a=1; b=0; c=0; hold=1; pulse=0; finish=0; rst=0; data=3; gate=1; phase=0;
    #10;
    go=0; a=0; b=1; c=0; hold=0; pulse=0; finish=0; data=9; gate=0; phase=1;
    #10;
    b=1; c=1; hold=1; pulse=1; finish=0; data=3; gate=1; phase=2;
    #10;
    b=0; c=0; hold=1; pulse=0; finish=1; data=3; gate=1; phase=3;
    #10;
    b=0; c=0; hold=1; pulse=0; finish=0; data=8; gate=1; phase=4;
    #10;
    phase=5;
    #10;
    $finish;
  end
  initial begin #7 rst=1; #1 rst=0; end

  ap_exact: assert property (@(posedge clk) go |-> ##3 pulse);
  ap_range: assert property (@(posedge clk) go |-> ##[1:3] pulse);
  ap_all: assert property (@(posedge clk) (a ##[1:2] b) |=> c);
  ap_first: assert property (@(posedge clk) first_match(a ##[1:2] b) |=> c);
  ap_throughout: assert property (@(posedge clk) go |-> hold throughout (a ##2 c));
  ap_and: assert property (@(posedge clk) go |-> ((a ##1 b) and (a ##3 finish)));
  ap_intersect: assert property (@(posedge clk) go |-> ((a ##1 b) intersect (a ##3 finish)));
  ap_within: assert property (@(posedge clk) go |-> ((b ##1 c) within (a ##3 finish)));
  ap_within_bad: assert property (@(posedge clk) go |-> ((b ##1 never_ack) within (a ##3 finish)));
  ap_repeat: assert property (@(posedge clk) go |-> (hold[*2] ##1 finish));
  ap_occurrence: assert property (@(posedge clk) go |-> (b[->2] ##1 c));
  ap_empty: assert property (@(posedge clk) go |-> (a[*0] ##2 b[*0]));
  ap_preponed: assert property (@(posedge clk) go |-> nba);
  ap_x: assert property (@(posedge clk) go |-> unknown_b);
  ap_case: assert property (@(posedge clk) go |-> (unknown_b===1'bx));
  ap_stable: assert property (@(posedge clk) go |=> $stable(data));
  ap_past: assert property (@(posedge clk) (phase==4) |-> data==$past(data,2,gate));
  ap_reset: assert property (@(posedge clk) disable iff(rst) go |=> never_ack);
  ap_sampled_reset: assert property (@(posedge clk) disable iff($sampled(rst)) go |=> never_ack);
  ap_until_right_pass: assert property (@(posedge clk) go |-> (never_ack until (go |=> b)));
  ap_until_right_fail: assert property (@(posedge clk) go |-> (never_ack until (go |=> c)));
  ap_until_left_fail: assert property (@(posedge clk) go |-> ((go |=> never_ack) until b));

  property tied;
    logic [3:0] v;
    first_match(((a,v=data) ##[1:2] b) or ((a,v=data+1) ##1 b)) |=> data==v;
  endproperty
  ap_tied: assert property (@(posedge clk) go |-> tied);
endmodule
