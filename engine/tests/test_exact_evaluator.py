"""Exact-match evaluator: a pure function, no model and no database."""

import pytest

from app.evaluators import EXACT_MISMATCH, evaluate_exact
from app.schemas import ExactConfig


def exact(expected, *, case_sensitive=None, normalize_whitespace=None) -> ExactConfig:  # type: ignore[no-untyped-def]
    return ExactConfig(expected=expected, case_sensitive=case_sensitive, normalize_whitespace=normalize_whitespace)


# --- matching --------------------------------------------------------------------------------


def test_identical_text_passes():
    ev = evaluate_exact("Your refund is approved.", exact("Your refund is approved."))
    assert ev.passed is True
    assert ev.quality_score == 1.0
    assert ev.failure_category is None and ev.failure_explanation is None
    assert ev.details == {
        "evaluator": "exact",
        "case_sensitive": False,
        "normalize_whitespace": False,
        "compared_length": {"response": 24, "expected": 24},
    }


def test_different_text_fails_with_binary_score():
    ev = evaluate_exact("Your refund is denied.", exact("Your refund is approved."))
    assert ev.passed is False
    assert ev.quality_score == 0.0
    assert ev.failure_category == EXACT_MISMATCH
    assert ev.failure_explanation == (
        "Response does not match the expected text: first difference at character 16 "
        "(response 22 chars, expected 24)."
    )
    assert ev.details["first_difference"] == {"index": 15, "line": 1, "column": 16}


def test_prefix_of_expected_fails_at_end_of_shorter_text():
    ev = evaluate_exact("Your refund is approved", exact("Your refund is approved."))
    assert not ev.passed
    assert ev.details["first_difference"]["index"] == 23


# --- case ------------------------------------------------------------------------------------


def test_case_insensitive_by_default():
    assert evaluate_exact("YOUR REFUND IS APPROVED.", exact("Your refund is approved.")).passed


def test_case_sensitive_fails_and_explains_case_only_difference():
    ev = evaluate_exact("your refund is approved.", exact("Your refund is approved.", case_sensitive=True))
    assert not ev.passed
    assert ev.details["differs_only_by_case"] is True
    assert ev.details["differs_only_by_whitespace"] is False
    assert ev.failure_explanation.endswith("The texts differ only in letter case.")


def test_case_folding_uses_casefold():
    assert evaluate_exact("STRASSE", exact("straße")).passed


# --- whitespace ------------------------------------------------------------------------------


def test_whitespace_is_significant_by_default_and_explained():
    ev = evaluate_exact("Your  refund is approved.\n", exact("Your refund is approved."))
    assert not ev.passed
    assert ev.details["differs_only_by_whitespace"] is True
    assert "differ only in whitespace (set normalize_whitespace to ignore it)" in ev.failure_explanation


def test_normalize_whitespace_collapses_runs_and_trims():
    cfg = exact("Your refund is approved.", normalize_whitespace=True)
    assert evaluate_exact("  Your\trefund \n\n is   approved.\n", cfg).passed
    assert evaluate_exact("Your refund is approved.", cfg).passed  # NBSP, em space


def test_normalize_whitespace_still_needs_the_words_to_match():
    ev = evaluate_exact("Your refund is  approved", exact("Your refund is approved.", normalize_whitespace=True))
    assert not ev.passed  # the missing period still counts
    assert ev.details["differs_only_by_whitespace"] is False


def test_whitespace_cannot_be_removed_entirely():
    assert not evaluate_exact("Yourrefund", exact("Your refund", normalize_whitespace=True)).passed


# --- multiline -------------------------------------------------------------------------------


def test_multiline_match():
    text = "Order #42\nStatus: shipped\nETA: 2 days"
    assert evaluate_exact(text, exact(text, case_sensitive=True)).passed


def test_multiline_mismatch_reports_line_and_column():
    ev = evaluate_exact("Order #42\nStatus: pending\nETA: 2 days", exact("Order #42\nStatus: shipped\nETA: 2 days"))
    assert not ev.passed
    assert ev.details["first_difference"] == {"index": 18, "line": 2, "column": 9}
    assert "first difference at line 2, column 9" in ev.failure_explanation


def test_line_endings_are_whitespace():
    assert not evaluate_exact("a\r\nb", exact("a\nb")).passed
    assert evaluate_exact("a\r\nb", exact("a\nb", normalize_whitespace=True)).passed


# --- empty, unicode, punctuation -------------------------------------------------------------


def test_empty_response_fails_clearly():
    ev = evaluate_exact("", exact("Your refund is approved."))
    assert not ev.passed
    assert ev.failure_category == EXACT_MISMATCH
    assert ev.failure_explanation == "Response is empty; expected exact text."
    assert ev.details["compared_length"] == {"response": 0, "expected": 24}


def test_whitespace_only_response_is_empty_after_normalisation():
    ev = evaluate_exact(" \n\t ", exact("Approved", normalize_whitespace=True))
    assert ev.failure_explanation == "Response is empty; expected exact text."


def test_unicode_text_matches():
    assert evaluate_exact("Réembolso aprobado ✅ 退款已批准", exact("Réembolso aprobado ✅ 退款已批准")).passed


def test_unicode_canonical_equivalence():
    composed, decomposed = "café", "café"  # same visible text, different code points
    assert evaluate_exact(decomposed, exact(composed, case_sensitive=True)).passed


@pytest.mark.parametrize(
    "response",
    ["Your refund is approved", "Your refund is approved!", "Your refund is approved..", "Your refund, is approved."],
)
def test_punctuation_differences_always_count(response):
    cfg = exact("Your refund is approved.", normalize_whitespace=True)  # even with normalisation
    assert not evaluate_exact(response, cfg).passed


def test_failure_metadata_does_not_copy_response_or_expected_text():
    secret_response = "Account 4111-1111-1111-1111 belongs to Jane Doe"
    expected = "Your refund is approved."
    ev = evaluate_exact(secret_response, exact(expected))
    blob = repr(ev.details) + ev.failure_explanation
    for fragment in ("4111", "Jane", "refund", "approved"):
        assert fragment not in blob
