"""The opt-in corpus must not credit unknown or absent evidence as a result."""
import pytest

from scripts.check_expression_faults import check_outcome, compare_samples


def test_only_observed_known_values_can_establish_a_mismatch():
    rows = [{"signals": {"a": {"dec": a}, "b": {"dec": b}}}
            for a, b in [(None, None), (None, 0), (0, None), (1, 1), (1, 2)]]
    known, bad = compare_samples(rows, "a", "b")
    assert known == rows[-2:]
    assert bad == rows[-1:]


@pytest.mark.parametrize("checks,expected", [
    ([], "match"),
    ([{"status": "match", "known_samples": 0}], "match"),
    ([{"status": "missing"}], "match"),
    ([{"status": "missing"}], "mismatch"),
    ([{"status": "match", "known_samples": 128}], "mismatch"),
    ([{"status": "mismatch", "known_samples": 128}], "match"),
    ([{"status": "mismatch", "known_samples": 128}], "missing"),
])
def test_absent_evidence_or_wrong_outcome_is_not_a_pass(checks, expected):
    assert not check_outcome(checks, expected)


def test_mixed_fault_and_clean_checks_require_positive_mismatch_evidence():
    checks = [{"status": "match", "known_samples": 128}, {"status": "mismatch", "known_samples": 128}]
    assert check_outcome(checks, "mismatch")
    assert check_outcome([{"status": "missing"}], "missing")
