"""The keyword evaluator is a pure function: no model, no database."""

import pytest

from app.evaluators import EVALUATORS, FORBIDDEN_CONTENT, KEYWORD_MISSING, evaluate_keywords
from app.schemas import KeywordConfig


def kw(required, forbidden=None, case_sensitive=None) -> KeywordConfig:  # type: ignore[no-untyped-def]
    return KeywordConfig(required=required, forbidden=forbidden, case_sensitive=case_sensitive)


def test_all_required_present_passes_with_full_score():
    ev = evaluate_keywords("Refunds are available within 30 days of purchase.", kw(["30 days", "refund"]))
    assert ev.passed is True
    assert ev.quality_score == 1.0
    assert ev.failure_category is None and ev.failure_explanation is None
    assert ev.details == {
        "evaluator": "keyword",
        "case_sensitive": False,
        "required": ["30 days", "refund"],
        "forbidden": [],
        "missing_required": [],
        "found_forbidden": [],
    }


def test_missing_required_keyword_fails():
    ev = evaluate_keywords("We offer refunds.", kw(["30 days", "refund"]))
    assert ev.passed is False
    assert ev.quality_score == 0.0
    assert ev.failure_category == KEYWORD_MISSING
    assert ev.failure_explanation == "Response missing required keyword '30 days'."
    assert ev.details["missing_required"] == ["30 days"]


def test_every_missing_keyword_is_reported():
    ev = evaluate_keywords("Hello!", kw(["30 days", "receipt"]))
    assert ev.failure_explanation == "Response missing required keywords '30 days', 'receipt'."
    assert ev.details["missing_required"] == ["30 days", "receipt"]


def test_forbidden_keyword_fails_even_if_required_present():
    ev = evaluate_keywords("Refunds within 30 days, guaranteed!", kw(["30 days"], forbidden=["guarantee"]))
    assert ev.passed is False
    assert ev.quality_score == 0.0
    assert ev.failure_category == FORBIDDEN_CONTENT
    assert ev.failure_explanation == "Response contains forbidden keyword 'guarantee'."
    assert ev.details["found_forbidden"] == ["guarantee"]


def test_forbidden_takes_precedence_but_both_problems_are_explained():
    ev = evaluate_keywords("Guaranteed refunds!", kw(["30 days"], forbidden=["guarantee"]))
    assert ev.failure_category == FORBIDDEN_CONTENT
    assert ev.failure_explanation == (
        "Response contains forbidden keyword 'guarantee' and missing required keyword '30 days'."
    )
    assert ev.details["missing_required"] == ["30 days"]


def test_matching_is_case_insensitive_by_default():
    assert evaluate_keywords("REFUNDS WITHIN 30 DAYS", kw(["refunds", "30 days"])).passed
    assert not evaluate_keywords("GUARANTEED", kw(["guaranteed"], forbidden=["Guaranteed"], case_sensitive=None)).passed


def test_case_insensitivity_uses_casefold():
    assert evaluate_keywords("Die STRASSE ist lang", kw(["straße"])).passed


def test_case_sensitive_required():
    cfg = kw(["Acme"], case_sensitive=True)
    assert evaluate_keywords("Welcome to Acme support", cfg).passed
    ev = evaluate_keywords("welcome to acme support", cfg)
    assert not ev.passed
    assert ev.failure_explanation == "Response missing required keyword 'Acme' (case-sensitive)."
    assert ev.details["case_sensitive"] is True


def test_case_sensitive_forbidden():
    cfg = kw(["ok"], forbidden=["SECRET"], case_sensitive=True)
    assert evaluate_keywords("ok, that is a secret", cfg).passed  # lowercase is allowed
    assert not evaluate_keywords("ok, that is a SECRET", cfg).passed


def test_matching_is_substring_containment():
    # Documented behaviour: no word boundaries, so "refund" matches "refunds".
    assert evaluate_keywords("refunds", kw(["refund"])).passed


def test_empty_response_fails_with_all_keywords_missing():
    ev = evaluate_keywords("", kw(["a", "b"]))
    assert not ev.passed
    assert ev.details["missing_required"] == ["a", "b"]


@pytest.mark.parametrize(
    "response,passed",
    [("Refunds within 30 days", True), ("No refunds", False), ("30 days, no guarantee", False)],
)
def test_registry_entry_takes_raw_config(response, passed):
    config = {"required": ["30 days"], "forbidden": ["guarantee"], "case_sensitive": False}
    assert EVALUATORS["keyword"](response, config).passed is passed


def test_only_keyword_is_registered_in_this_milestone():
    assert set(EVALUATORS) == {"keyword"}
