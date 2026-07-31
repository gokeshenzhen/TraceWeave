import pytest

from src.problem_hints import compute_problem_hints, compute_xprop_priority_for_group


def test_compute_hints_detects_x():
    events = [
        {
            "failure_mechanism": "xprop",
            "message_text": "X detected",
            "group_signature": "UVM_ERROR [CHK]",
        }
    ]
    summary = {"groups": [{"first_time_ps": 1000}]}
    hints = compute_problem_hints(summary, events)
    assert hints.has_x is True
    assert hints.has_z is False
    assert hints.first_error_time_ps == 1000


def test_compute_hints_detects_z():
    events = [
        {
            "failure_mechanism": "unknown",
            "message_text": "high-Z on bus",
            "group_signature": "ERROR",
        }
    ]
    summary = {"groups": [{"first_time_ps": 2000}]}
    hints = compute_problem_hints(summary, events)
    assert hints.has_x is False
    assert hints.has_z is True
    assert hints.error_pattern == "zprop"


def test_compute_hints_no_errors():
    hints = compute_problem_hints({"groups": []}, [])
    assert hints.has_x is False
    assert hints.has_z is False
    assert hints.first_error_time_ps is None
    assert hints.error_pattern is None


def test_compute_hints_mismatch_pattern():
    events = [
        {
            "failure_mechanism": "mismatch",
            "message_text": "expected 0x1 got 0x2",
            "group_signature": "UVM_ERROR [SCB]",
        }
    ]
    summary = {"groups": [{"first_time_ps": 500}]}
    hints = compute_problem_hints(summary, events)
    assert hints.has_x is False
    assert hints.has_z is False
    assert hints.error_pattern == "mismatch"


def test_compute_hints_detects_x_in_hex_actual():
    events = [
        {
            "failure_mechanism": "mismatch",
            "message_text": "Expected 82dcbafbdeab6602 Got 8X00a2Xcab814ebd",
            "group_signature": "ERROR: comparison",
            "expected": "82dcbafbdeab6602",
            "actual": "8X00a2Xcab814ebd",
        }
    ]
    summary = {"groups": [{"first_time_ps": 10100000}]}
    hints = compute_problem_hints(summary, events)
    assert hints.has_x is True
    assert hints.has_z is False
    assert hints.error_pattern == "xprop"


def test_compute_hints_detects_x_in_pure_unknown_actual():
    events = [
        {
            "failure_mechanism": "mismatch",
            "message_text": "Expected FF Got XX",
            "group_signature": "ERROR: comparison",
            "expected": "FF",
            "actual": "XX",
        }
    ]
    summary = {"groups": [{"first_time_ps": 7000}]}
    hints = compute_problem_hints(summary, events)
    assert hints.has_x is True
    assert hints.has_z is False
    assert hints.error_pattern == "xprop"


def test_compute_hints_detects_z_in_hex_actual():
    events = [
        {
            "failure_mechanism": "mismatch",
            "message_text": "Expected FF Got ZZ",
            "group_signature": "ERROR: comparison",
            "expected": "FF",
            "actual": "ZZ",
        }
    ]
    summary = {"groups": [{"first_time_ps": 5000}]}
    hints = compute_problem_hints(summary, events)
    assert hints.has_x is False
    assert hints.has_z is True
    assert hints.error_pattern == "zprop"


@pytest.mark.parametrize(
    ("value", "has_x", "has_z", "error_pattern"),
    [
        ("xx", True, False, "xprop"),
        ("8'hx3", True, False, "xprop"),
        ("0xXf", True, False, "xprop"),
        ("zz", False, True, "zprop"),
        ("next_state", False, False, "tb_error"),
    ],
)
def test_compute_hints_detects_unknown_in_structured_value(
    value, has_x, has_z, error_pattern
):
    events = [
        {
            "failure_mechanism": "tb_error",
            "message_text": "DEEP_X_EXPECTED",
            "group_signature": "ERROR",
            "structured_fields": {"value": value},
        }
    ]

    hints = compute_problem_hints(
        {"groups": [{"first_time_ps": 66_000}]}, events
    )

    assert hints.has_x is has_x
    assert hints.has_z is has_z
    assert hints.error_pattern == error_pattern


def test_compute_hints_does_not_treat_identifier_fields_as_unknown_values():
    events = [
        {
            "failure_mechanism": "tb_error",
            "message_text": "DEEP_X_EXPECTED",
            "group_signature": "ERROR",
            "structured_fields": {"signal": "x_axis", "value": "next_state"},
        }
    ]

    hints = compute_problem_hints({"groups": []}, events)

    assert hints.has_x is False
    assert hints.has_z is False
    assert hints.error_pattern == "tb_error"


def test_xprop_priority_uses_structured_value_evidence():
    priority = compute_xprop_priority_for_group(
        [
            {
                "message_text": "DEEP_X_EXPECTED",
                "group_signature": "ERROR",
                "structured_fields": {"VALUE": "XX"},
            }
        ],
        global_has_x=True,
        global_has_z=False,
    )

    assert priority == "high"


def test_compute_xprop_priority_returns_none_when_globally_irrelevant():
    assert compute_xprop_priority_for_group([], global_has_x=False, global_has_z=False) is None


def test_compute_xprop_priority_detects_x_event():
    priority = compute_xprop_priority_for_group(
        [{"actual": "8X00a2Xcab814ebd", "message_text": "compare fail", "group_signature": "ERR"}],
        global_has_x=True,
        global_has_z=False,
    )
    assert priority == "high"


def test_compute_xprop_priority_detects_z_event():
    priority = compute_xprop_priority_for_group(
        [{"actual": "ZZ", "message_text": "compare fail", "group_signature": "ERR"}],
        global_has_x=False,
        global_has_z=True,
    )
    assert priority == "high"


def test_compute_xprop_priority_returns_normal_for_clean_group_when_global_x_exists():
    priority = compute_xprop_priority_for_group(
        [{"actual": "0x12", "expected": "0x34", "message_text": "compare fail", "group_signature": "ERR"}],
        global_has_x=True,
        global_has_z=False,
    )
    assert priority == "normal"


def test_compute_xprop_priority_ignores_derived_failure_mechanism_without_raw_xz_evidence():
    priority = compute_xprop_priority_for_group(
        [
            {
                "failure_mechanism": "xprop",
                "actual": "0x12",
                "expected": "0x34",
                "message_text": "compare fail",
                "group_signature": "ERR",
            }
        ],
        global_has_x=True,
        global_has_z=False,
    )
    assert priority == "normal"
