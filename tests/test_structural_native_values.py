import pytest

from src.structural_native_values import operate, value, vector


@pytest.mark.parametrize('op,args,expected', [
    ('npiBitAndOp', ['????', '0000'], '0000'),
    ('npiBitAndOp', ['????', '1111'], '????'),
    ('npiBitAndOp', ['xz01', '1010'], 'x000'),
    ('npiBitOrOp', ['????', '1111'], '1111'),
    ('npiBitXorOp', ['????', 'xxxx'], 'xxxx'),
    ('npiBitNegOp', ['z?10'], 'x?01'),
    ('npiNotOp', ['1?x'], '0'),
    ('npiNotOp', ['00xz'], 'x'),
    ('npiNotOp', ['????'], '?'),
    ('npiLogAndOp', ['????', '0000'], '0'),
    ('npiLogOrOp', ['????', '1x00'], '1'),
    ('npiUnaryAndOp', ['0?xz'], '0'),
    ('npiUnaryOrOp', ['1?xz'], '1'),
    ('npiUnaryXorOp', ['x???'], 'x'),
    ('npiUnaryNandOp', ['1111'], '0'),
    ('npiConditionOp', ['?', '10xz', '10xz'], '10xz'),
    ('npiConditionOp', ['x', '10z0', '11z1'], '1xzx'),
    ('npiConditionOp', ['?', '10xz', '10xx'], '10x?'),
])
def test_four_state_transfer(op, args, expected):
    assert operate(op, args) == expected


def test_dynamic_unknown_is_not_a_constant_x_and_regions_round_trip():
    size, regions, complete = value('10?xz?01')
    assert not complete
    assert regions == [dict(lsb_offset=0, width=2, bits='01'),
                       dict(lsb_offset=3, width=2, bits='xz'), dict(lsb_offset=6, width=2, bits='10')]
    assert vector(size, regions) == '10?xz?01'
    assert operate('npiAddOp', ['????', '0000']) is None
