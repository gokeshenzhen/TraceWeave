import cocotb
from cocotb.triggers import Timer
@cocotb.test()
async def test_data(dut):
    dut.a.value = 0x10
    await Timer(1.25, units='ns')
    assert int(dut.y.value) == 0x10, f'expected=0x10 actual={int(dut.y.value):#x}'
