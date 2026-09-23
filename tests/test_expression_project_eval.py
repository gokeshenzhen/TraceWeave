"""Prevent false passes in the optional external-project corpus checker."""
import pytest

from scripts.expression_project_eval import checker_result


@pytest.mark.parametrize("project,log", [
    ("P01", "Checked 1000 stimuli\n" * 5),
    ("P02", "TW_RR_COMPLETED\nLine: 2 is unfair!"),
    ("P03", "Checked 512 stimuli\nError: Mismatch, Expected: ff Got 0"),
    ("P04", "TW_WATCHDOG"),
    ("P05", "Passed: 10000\nFailed: 1"),
    ("P06", "Randomization failed\nTW_POPCOUNT_COMPLETED"),
    ("P09", "All 4 test cases completed successfully\nError! Got: 0x1234"),
    ("P10", "All 4 test cases completed successfully\n*** ERROR: TC 0 NOT successful."),
    ("P11", "ERROR: Expected a5, received xx\nTestbench done"),
])
def test_completion_or_zero_exit_is_insufficient(project, log):
    assert not checker_result(project, log)["passed"]


def test_expected_decoder_overlap_warning_is_not_a_failure():
    assert checker_result("P05", "Warning: Overlapping address region\nPassed: 12345\nFailed: 0")["passed"]


def test_all_fifo_configurations_are_required():
    assert checker_result("P01", "Checked 1000 stimuli\n" * 6)["passed"]
