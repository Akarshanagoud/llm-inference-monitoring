"""Response validators."""
from llmobs import ResponseValidator
from llmobs.validation import (
    check_no_repetition,
    check_not_truncated,
    check_valid_json,
)


def test_empty_response_fails():
    result = ResponseValidator().validate("   ")
    assert not result.valid
    assert "non_empty" in result.checks_failed


def test_normal_response_passes():
    result = ResponseValidator().validate("The refund window is 30 days.")
    assert result.valid


def test_length_finish_reason_warns_about_truncation():
    issues = check_not_truncated("some text that got cut off", {"finish_reason": "length"})
    assert issues and issues[0].severity == "warn"


def test_missing_terminal_punctuation_warns():
    text = "this is a long response that simply stops without any final punctuation mark"
    issues = check_not_truncated(text, {})
    assert issues


def test_complete_sentence_does_not_warn():
    assert check_not_truncated("A complete sentence ends here.", {}) == []


def test_degenerate_repetition_fails():
    text = ("the model repeats itself over and over " * 5)
    issues = check_no_repetition(text, {})
    assert issues and issues[0].severity == "fail"


def test_varied_text_passes_repetition_check():
    text = (
        "Retrieval augmented generation grounds responses in source documents, "
        "which reduces hallucination and lets you cite where each claim came from."
    )
    assert check_no_repetition(text, {}) == []


def test_low_diversity_warns():
    text = " ".join(["alpha beta gamma delta"] * 3 + ["alpha"] * 40)
    issues = check_no_repetition(text, {})
    assert issues


def test_refusal_is_flagged_as_a_warning_not_a_failure():
    result = ResponseValidator().validate("I'm sorry, I can't help with that request.")
    assert result.valid  # refusals are legitimate
    assert any(i.check == "no_refusal" for i in result.issues)


def test_json_check_only_runs_when_expected():
    assert check_valid_json("not json at all", {}) == []
    assert check_valid_json("not json at all", {"expect_json": True})


def test_fenced_json_is_accepted():
    assert check_valid_json('```json\n{"ok": true}\n```', {"expect_json": True}) == []


def test_required_phrase_missing_fails():
    result = ResponseValidator().validate("Some answer.", must_contain=["policy 4.2"])
    assert not result.valid
    assert "required_phrases" in result.checks_failed


def test_forbidden_phrase_present_fails():
    result = ResponseValidator().validate(
        "Contact us at internal-only@corp.example.", must_not_contain=["internal-only"]
    )
    assert not result.valid


def test_length_bounds_enforced():
    result = ResponseValidator().validate("short.", min_chars=50)
    assert not result.valid


def test_custom_validator_can_be_added():
    def no_emoji(text, _context):
        from llmobs.validation import ValidationIssue

        if any(ord(c) > 0x1F000 for c in text):
            return [ValidationIssue("no_emoji", "fail", "emoji present")]
        return []

    validator = ResponseValidator()
    validator.add("no_emoji", no_emoji)
    assert not validator.validate("nice work \U0001F600").valid
    assert validator.validate("nice work").valid


def test_validator_can_be_removed():
    validator = ResponseValidator()
    assert validator.remove("non_empty")
    assert validator.validate("   ").valid


def test_raising_validator_degrades_to_a_warning():
    def broken(_text, _context):
        raise RuntimeError("check exploded")

    validator = ResponseValidator([("broken", broken)])
    result = validator.validate("anything")
    assert result.valid  # a broken check must not fail the response
    assert result.issues[0].severity == "warn"
