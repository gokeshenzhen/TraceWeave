module oracle;
timeunit 1ns; timeprecision 1ps;
bit clk=0, rst=0;
int c=0, shared_count=0;
always #5 clk=~clk;
always @(negedge clk) c=c+1;
initial begin #7 rst=1; #1 rst=0; end
default clocking @(posedge clk); endclocking
function automatic int twice(input int x); return x*2; endfunction
function void bump(); shared_count=shared_count+1; endfunction
property captured; int v; (c<2, v=c) |-> ##2 (c==v+2); endproperty
cap: assert property(captured) else $error("OBS FAIL capture %0t sampled=%0d", $time, $sampled(c));
property side_effect; int v; (c==0, v=shared_count, bump()) |=> shared_count==v+1; endproperty
side: assert property(side_effect) else $error("OBS FAIL side_effect %0t", $time);
ap_overlap: assert property ((c==0) |-> (c==0)) else $error("OBS FAIL overlap %0t sampled=%0d", $time, $sampled(c));
ap_nonoverlap: assert property ((c==0) |=> (c==1)) else $error("OBS FAIL nonoverlap %0t sampled=%0d", $time, $sampled(c));
ap_offset: assert property ((c==0) |=> ##2 (c==3)) else $error("OBS FAIL offset %0t sampled=%0d", $time, $sampled(c));
ap_all_matches: assert property (((c==0) ##[1:2] 1) |-> (c==2)) else $error("OBS FAIL all_matches %0t sampled=%0d", $time, $sampled(c));
ap_first_match: assert property (first_match((c==0) ##[1:2] 1) |-> (c==2)) else $error("OBS FAIL first_match %0t sampled=%0d", $time, $sampled(c));
ap_any_consequent: assert property ((c==0) |-> ##[1:2] (c==2)) else $error("OBS FAIL any_consequent %0t sampled=%0d", $time, $sampled(c));
ap_seq_and: assert property ((c==0) |-> ((1 ##1 1) and (1 ##2 1))) else $error("OBS FAIL seq_and %0t sampled=%0d", $time, $sampled(c));
ap_intersect_bad: assert property ((c==0) |-> ((1 ##1 1) intersect (1 ##2 1))) else $error("OBS FAIL intersect_bad %0t sampled=%0d", $time, $sampled(c));
ap_throughout_bad: assert property ((c==0) |-> ((c!=1) throughout (1 ##2 1))) else $error("OBS FAIL throughout_bad %0t sampled=%0d", $time, $sampled(c));
ap_within_ok: assert property ((c==0) |-> ((c==1) within (1 ##2 1))) else $error("OBS FAIL within_ok %0t sampled=%0d", $time, $sampled(c));
ap_within_bad: assert property ((c==0) |-> ((c==3) within (1 ##2 1))) else $error("OBS FAIL within_bad %0t sampled=%0d", $time, $sampled(c));
ap_consecutive_bad: assert property ((c==0) |-> ((c==0 || c==2)[*2])) else $error("OBS FAIL consecutive_bad %0t sampled=%0d", $time, $sampled(c));
ap_goto_ok: assert property ((c==0) |-> ((c==0 || c==2)[->2] ##1 (c==3))) else $error("OBS FAIL goto_ok %0t sampled=%0d", $time, $sampled(c));
ap_nonconsecutive_tail: assert property ((c==0) |-> ((c==0 || c==2)[=2] ##1 (c==4))) else $error("OBS FAIL nonconsecutive_tail %0t sampled=%0d", $time, $sampled(c));
ap_goto_tail_bad: assert property ((c==0) |-> ((c==0 || c==2)[->2] ##1 (c==4))) else $error("OBS FAIL goto_tail_bad %0t sampled=%0d", $time, $sampled(c));
ap_empty: assert property ((c==0) |-> (1[*0] ##1 (c==0))) else $error("OBS FAIL empty %0t sampled=%0d", $time, $sampled(c));
ap_conditional: assert property ((c==0) |-> if (c==0) (1 ##1 (c==1)) else 0) else $error("OBS FAIL conditional %0t sampled=%0d", $time, $sampled(c));
ap_disabled: assert property (disable iff (rst) (c==0) |-> ##2 0) else $error("OBS FAIL disabled %0t sampled=%0d", $time, $sampled(c));
ap_vacuous: assert property ((c==99) |-> 0) else $error("OBS FAIL vacuous %0t sampled=%0d", $time, $sampled(c));
ap_weak_pending: assert property ((c==0) |-> weak(1 ##[1:$] (c==99))) else $error("OBS FAIL weak_pending %0t sampled=%0d", $time, $sampled(c));
ap_strong_pending: assert property ((c==0) |-> strong(1 ##[1:$] (c==99))) else $error("OBS FAIL strong_pending %0t sampled=%0d", $time, $sampled(c));
ap_pure_function: assert property ((c==0) |=> twice(c)==2) else $error("OBS FAIL pure_function %0t sampled=%0d", $time, $sampled(c));
assume_bad: assume property ((c==0) |=> (c==2)) else $error("OBS FAIL assume %0t sampled=%0d", $time, $sampled(c));
assert_bad: assert property ((c==0) |=> (c==2)) else $error("OBS FAIL assert %0t sampled=%0d", $time, $sampled(c));
cover_match: cover property ((c==0) ##2 (c==2)) $display("OBS COVER match %0t sampled=%0d", $time, $sampled(c));
initial begin $timeformat(-9,0,"",1); $dumpfile("oracle.vcd"); $dumpvars(0,oracle); #80; $display("OBS END shared=%0d", shared_count); $finish; end
endmodule
