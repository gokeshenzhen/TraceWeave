module clocks;
timeunit 1ns; timeprecision 1ps;
bit a=0,b=0,d=0;
int c=0;
logic [1:0] v=2'bxx;
always #5 a=~a;
initial begin #5 b=1; forever #10 b=~b; end
initial begin #8 d=1; forever #10 d=~d; end
always @(negedge a) begin c=c+1; case(c) 1:v=2'b10; 2:v=2'b11; 3:v=2'b01; default:v=0; endcase end
c0: cover property (@(posedge a) (c==0) ##0 @(posedge b) 1) $display("OBS COVER c0 %0t",$time);
c1: cover property (@(posedge a) (c==0) ##1 @(posedge b) 1) $display("OBS COVER c1 %0t",$time);
cd: cover property (@(posedge a) (c==0) ##0 @(posedge d) 1) $display("OBS COVER cd %0t",$time);
imp: assert property (@(posedge a) (c==0) |=> @(posedge d) 1) if($time<10) $display("OBS PASS imp %0t",$time);
property cap; int x; @(posedge a) (c==0,x=c) |-> @(posedge b) (x==0); endproperty
cp: assert property(cap) else $error("OBS FAIL cap %0t",$time);
rose: cover property (@(posedge a) $rose(v)) $display("OBS COVER rose %0t",$time);
fell: cover property (@(posedge a) $fell(v)) $display("OBS COVER fell %0t",$time);
stable: cover property (@(posedge a) c==3 && $stable(v)) $display("OBS COVER stable %0t",$time);
all_late: assert property (@(posedge a) ((c==0) ##[1:2] 1) |-> c==1) else $error("OBS FAIL all_late %0t",$time);
first_early: assert property (@(posedge a) first_match((c==0) ##[1:2] 1) |-> c==1) else $error("OBS FAIL first_early %0t",$time);
property branch; int x; @(posedge a) (c==0) |-> (((1,x=1) ##1 1) or ((1,x=2) ##2 1)) ##0 (x==2); endproperty
br: assert property(branch) else $error("OBS FAIL branch %0t",$time);
property recursive(p); p and (1 |=> recursive(p)); endproperty
rec: assert property (@(posedge a) recursive(c<4)) else $error("OBS FAIL recursive %0t",$time);
initial begin $timeformat(-9,0,"",1); $dumpfile("clocks.vcd"); $dumpvars(0,clocks); #46; $finish; end
endmodule
