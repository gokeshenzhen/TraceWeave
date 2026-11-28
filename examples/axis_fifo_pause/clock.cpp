#include "Vtb.h"
#include "verilated.h"
#include "verilated_vcd_c.h"

int main(int argc, char** argv) {
    VerilatedContext context;
    context.commandArgs(argc, argv);
    context.traceEverOn(true);
    Vtb tb{&context};
    VerilatedVcdC trace;
    tb.trace(&trace, 2);
    trace.open("wave.vcd");

    tb.clk = 0;
    for (int half_cycle = 0; half_cycle <= 40 && !context.gotFinish(); ++half_cycle) {
        tb.eval();
        trace.dump(context.time());
        trace.flush();  // Keep the last complete rising edge even if the checker calls $fatal.
        context.timeInc(5000);  // Half of the 10 ns clock period, in 1 ps simulator ticks.
        tb.clk = !tb.clk;
    }
    tb.final();
    trace.close();
    return context.gotFinish() ? 0 : 1;
}
