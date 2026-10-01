#include "Vnative.h"
#include "verilated.h"
int main(int argc,char** argv) {
  VerilatedContext ctx; ctx.commandArgs(argc,argv);
  Vnative dut{&ctx}; dut.a=0x10; dut.clk=0; dut.eval();
  ctx.time(1250); dut.clk=1; dut.eval();
  dut.final(); return 0;
}
