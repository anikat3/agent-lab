"""Deterministic evaluators: pure functions from (response, eval_config) to an Evaluation.

No I/O, no LLM. Each evaluator receives a config that already passed the launch-time
validation in app/eval_config.py, and parses it again into its typed model.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.schemas import KeywordConfig

# failure_category values. The dashboard groups failures by these, so keep them stable.
KEYWORD_MISSING = "keyword_missing"
FORBIDDEN_CONTENT = "forbidden_content"


@dataclass(frozen=True)
class Evaluation:
    passed: bool
    quality_score: float
    failure_category: str | None = None
    failure_explanation: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def evaluate_keywords(response: str, config: KeywordConfig) -> Evaluation:
    """Pass iff every required keyword appears in the response and no forbidden one does.

    Matching is plain substring containment ("30 days" matches "within 30 days."), using
    str.casefold() unless case_sensitive is true. Score is binary: 1.0 pass, 0.0 fail.
    """
    case_sensitive = bool(config.case_sensitive)
    forbidden_terms = config.forbidden or []

    def norm(text: str) -> str:
        return text if case_sensitive else text.casefold()

    haystack = norm(response)
    missing = [k for k in config.required if norm(k) not in haystack]
    forbidden_found = [k for k in forbidden_terms if norm(k) in haystack]
    details: dict[str, Any] = {
        "evaluator": "keyword",
        "case_sensitive": case_sensitive,
        "required": list(config.required),
        "forbidden": list(forbidden_terms),
        "missing_required": missing,
        "found_forbidden": forbidden_found,
    }

    if not missing and not forbidden_found:
        return Evaluation(passed=True, quality_score=1.0, details=details)

    problems = []
    if forbidden_found:
        problems.append(f"contains forbidden {_plural('keyword', forbidden_found)} {_quote(forbidden_found)}")
    if missing:
        problems.append(f"missing required {_plural('keyword', missing)} {_quote(missing)}")
    return Evaluation(
        passed=False,
        quality_score=0.0,
        # Forbidden content wins when both apply: saying something prohibited is the more
        # serious failure. Both problems are still listed in the explanation and details.
        failure_category=FORBIDDEN_CONTENT if forbidden_found else KEYWORD_MISSING,
        failure_explanation="Response " + " and ".join(problems)
        + (" (case-sensitive)" if case_sensitive else "") + ".",
        details=details,
    )


def _plural(word: str, items: list[str]) -> str:
    return word if len(items) == 1 else word + "s"


def _quote(items: list[str]) -> str:
    return ", ".join(repr(i) for i in items)


def _keyword(response: str, config: dict[str, Any]) -> Evaluation:
    return evaluate_keywords(response, KeywordConfig.model_validate(config))


# eval_type -> evaluator. The engine advertises (and accepts at launch) exactly these.
EVALUATORS: dict[str, Callable[[str, dict[str, Any]], Evaluation]] = {
    "keyword": _keyword,
}
