from src.problem_hints import compute_problem_hints


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
