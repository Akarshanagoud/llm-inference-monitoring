"""Response validation.

Serving metrics tell you the model answered quickly. They do not tell you it
answered *usefully*. A model that has silently started emitting empty strings,
or truncating at the token limit, or looping the same phrase, looks perfectly
healthy on a latency dashboard — throughput may even improve.

These validators are cheap enough to run on every response and are exported as
metrics, so quality regressions show up on the same graph as latency
regressions.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ValidationIssue:
    check: str
    severity: str  # "warn" | "fail"
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class ValidationResult:
    valid: bool
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def failed(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "fail"]

    @property
    def checks_failed(self) -> list[str]:
        return [i.check for i in self.failed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "issues": [i.to_dict() for i in self.issues],
        }


Validator = Callable[[str, dict[str, Any]], list[ValidationIssue]]


def check_non_empty(text: str, _context: dict[str, Any]) -> list[ValidationIssue]:
    if text.strip():
        return []
    return [ValidationIssue("non_empty", "fail", "response is empty or whitespace only")]


def check_not_truncated(text: str, context: dict[str, Any]) -> list[ValidationIssue]:
    """A completion that stopped because it hit the token cap is suspect.

    ``finish_reason == "length"`` is the authoritative signal; the sentence
    heuristic catches backends that do not report one.
    """
    issues: list[ValidationIssue] = []
    if context.get("finish_reason") == "length":
        issues.append(
            ValidationIssue(
                "not_truncated", "warn", "generation stopped at the token limit",
                {"max_tokens": context.get("max_tokens")},
            )
        )
    elif text and text.strip()[-1] not in ".!?\"')}]`" and len(text) > 40:
        issues.append(
            ValidationIssue(
                "not_truncated", "warn", "response does not end at a sentence boundary",
                {"tail": text[-30:]},
            )
        )
    return issues


_REPEAT_WINDOW = 6


def check_no_repetition(text: str, _context: dict[str, Any]) -> list[ValidationIssue]:
    """Detect degenerate looping — the classic symptom of a bad sampling config."""
    words = text.split()
    if len(words) < _REPEAT_WINDOW * 3:
        return []

    seen: dict[tuple[str, ...], int] = {}
    for i in range(len(words) - _REPEAT_WINDOW + 1):
        window = tuple(words[i : i + _REPEAT_WINDOW])
        seen[window] = seen.get(window, 0) + 1

    worst_window, worst_count = max(seen.items(), key=lambda kv: kv[1])
    if worst_count >= 3:
        return [
            ValidationIssue(
                "no_repetition", "fail",
                f"{_REPEAT_WINDOW}-gram repeated {worst_count} times",
                {"phrase": " ".join(worst_window)[:80], "repeats": worst_count},
            )
        ]

    unique_ratio = len(set(words)) / len(words)
    if unique_ratio < 0.25:
        return [
            ValidationIssue(
                "no_repetition", "warn",
                f"low lexical diversity ({unique_ratio:.0%} unique tokens)",
                {"unique_ratio": round(unique_ratio, 3)},
            )
        ]
    return []


def check_no_refusal(text: str, _context: dict[str, Any]) -> list[ValidationIssue]:
    """Track refusal rate as a metric.

    A refusal is not a bug, but a *spike* in refusals after a prompt-template
    change usually is, and it is invisible unless you count them.
    """
    patterns = (
        r"(?i)\bi (?:can'?t|cannot|am unable to|won'?t) (?:help|assist|provide|do)\b",
        r"(?i)\bi'?m (?:sorry|afraid)[, ].{0,30}\b(?:can'?t|cannot|unable)\b",
        r"(?i)\bas an ai (?:language )?model\b",
    )
    for pattern in patterns:
        if re.search(pattern, text):
            return [
                ValidationIssue(
                    "no_refusal", "warn", "response appears to be a refusal",
                    {"pattern": pattern},
                )
            ]
    return []


def check_valid_json(text: str, context: dict[str, Any]) -> list[ValidationIssue]:
    """Only runs when the caller declared it expected JSON."""
    if not context.get("expect_json"):
        return []
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()
    try:
        json.loads(candidate)
    except json.JSONDecodeError as exc:
        return [
            ValidationIssue(
                "valid_json", "fail", f"expected JSON but parsing failed: {exc.msg}",
                {"position": exc.pos},
            )
        ]
    return []


def check_length_bounds(text: str, context: dict[str, Any]) -> list[ValidationIssue]:
    minimum = context.get("min_chars", 0)
    maximum = context.get("max_chars")
    issues: list[ValidationIssue] = []
    if minimum and len(text) < minimum:
        issues.append(
            ValidationIssue(
                "length_bounds", "fail", f"response shorter than {minimum} characters",
                {"length": len(text)},
            )
        )
    if maximum and len(text) > maximum:
        issues.append(
            ValidationIssue(
                "length_bounds", "warn", f"response longer than {maximum} characters",
                {"length": len(text)},
            )
        )
    return issues


def check_required_phrases(text: str, context: dict[str, Any]) -> list[ValidationIssue]:
    required = context.get("must_contain") or []
    missing = [phrase for phrase in required if phrase.lower() not in text.lower()]
    if missing:
        return [
            ValidationIssue(
                "required_phrases", "fail", f"missing required content: {missing}",
                {"missing": missing},
            )
        ]
    return []


def check_forbidden_phrases(text: str, context: dict[str, Any]) -> list[ValidationIssue]:
    forbidden = context.get("must_not_contain") or []
    present = [phrase for phrase in forbidden if phrase.lower() in text.lower()]
    if present:
        return [
            ValidationIssue(
                "forbidden_phrases", "fail", f"response contains forbidden content: {present}",
                {"present": present},
            )
        ]
    return []


DEFAULT_VALIDATORS: list[tuple[str, Validator]] = [
    ("non_empty", check_non_empty),
    ("not_truncated", check_not_truncated),
    ("no_repetition", check_no_repetition),
    ("no_refusal", check_no_refusal),
    ("valid_json", check_valid_json),
    ("length_bounds", check_length_bounds),
    ("required_phrases", check_required_phrases),
    ("forbidden_phrases", check_forbidden_phrases),
]


class ResponseValidator:
    """Runs a configurable set of checks over each response."""

    def __init__(self, validators: list[tuple[str, Validator]] | None = None) -> None:
        self.validators = list(validators if validators is not None else DEFAULT_VALIDATORS)

    def add(self, name: str, validator: Validator) -> None:
        self.validators.append((name, validator))

    def remove(self, name: str) -> bool:
        before = len(self.validators)
        self.validators = [v for v in self.validators if v[0] != name]
        return len(self.validators) < before

    def validate(self, text: str, **context: Any) -> ValidationResult:
        issues: list[ValidationIssue] = []
        for name, validator in self.validators:
            try:
                issues.extend(validator(text, context))
            except Exception as exc:  # noqa: BLE001 - a bad check must not 500 the request
                issues.append(
                    ValidationIssue(name, "warn", f"validator raised: {exc}")
                )
        return ValidationResult(valid=not any(i.severity == "fail" for i in issues), issues=issues)
