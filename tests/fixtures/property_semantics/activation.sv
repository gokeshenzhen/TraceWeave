module activation;
timeunit 1ns; timeprecision 1ps;
bit clk=0; int c=0; int raw=0;
always #5 clk=~clk;
always @(negedge clk) c=c+1;
always @(posedge clk) raw<=raw+1;
default clocking @(posedge clk); endclocking
ap: assert property(c==0 |=> c==1) $display("OBS PASS nonvac %0t sampled_c=%0d",$time,$sampled(c));
av: assert property(c==99 |=> 0) $display("OBS PASS vacuous %0t",$time);
sampled: assert property(c==0 |-> raw==0)
    if($sampled(c)==0) $display("OBS SAMPLE %0t sampled=%0d raw=%0d",$time,$sampled(raw),raw);
initial begin
    #1;
    expect(@(posedge clk) c==0 ##2 c==2) $display("OBS EXPECT %0t c=%0d",$time,c);
    else $error("OBS FAIL expect %0t",$time);
    $display("OBS RESUMED %0t",$time);
end
initial begin
    #1; assume property(@(posedge clk) c==0 |=> c==1) $display("OBS ASSUME %0t",$time);
end
initial begin $timeformat(-9,0,"",1); $dumpfile("activation.vcd"); $dumpvars(0,activation); #40; $finish; end
endmodule
