"""The Settings view's one source of validation (settings redesign spec §3): the
rules are exactly the config model's, the messages are the table's."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from diffbell.config import Config, DebounceConfig, DiffConfig, NotifyConfig
from diffbell.web.settings_check import (
    MESSAGES,
    CheckResult,
    Issue,
    SettingsRaw,
    check_settings,
    message_for,
    settings_values,
)

NUMBER = "Enter a number of seconds."
POSITIVE = "Must be more than 0 seconds."
LINES = "Enter 0 to 200 lines."
TIME = "Use a time like 23:00."
DIFFERS = "Must differ from the start time."
MACHINE = "Use at most 40 characters, without control characters."


def raw(**changes: Any) -> SettingsRaw:
    fields: dict[str, Any] = {
        "quiet_seconds": "10",
        "max_wait_seconds": "60",
        "max_lines": "20",
        "defaults": [],
        "ignore": [],
        "redact": [],
        "quiet_hours": None,
        "heartbeat": "09:00",
    }
    fields.update(changes)
    return SettingsRaw(**fields)


def test_the_table_holds_the_specs_sentences() -> None:
    assert MESSAGES == {
        "seconds_number": NUMBER,
        "seconds_positive": POSITIVE,
        "seconds_at_least_quiet": "Must be at least the quiet time ({quiet} seconds).",
        "lines_range": LINES,
        "time_format": TIME,
        "time_differs": DIFFERS,
        "machine_name": MACHINE,
    }


def test_a_valid_body_is_empty() -> None:
    result = check_settings(raw())
    assert result.ok and result.body() == {"errors": {}, "issues": {}}
    assert result == CheckResult()


@pytest.mark.parametrize(
    ("typed", "message"),
    [
        ("", NUMBER),
        ("   ", NUMBER),
        ("abc", NUMBER),
        ("inf", NUMBER),
        ("nan", NUMBER),
        ("1_000", NUMBER),
        ("1e999", NUMBER),
        ("١٠", NUMBER),  # other scripts' digits
        ("0", POSITIVE),
        ("-1", POSITIVE),
        ("0.0", POSITIVE),
        (0, POSITIVE),
        (-2.5, POSITIVE),
    ],
)
def test_the_quiet_time_must_be_a_number_above_zero(typed: str | float, message: str) -> None:
    errors = check_settings(raw(quiet_seconds=typed, max_wait_seconds="60")).errors
    assert errors == {"quiet_seconds": message}


@pytest.mark.parametrize("typed", ["2.5", " 2.5 ", ".5", "10.", "1e1", 2.5, 10])
def test_floats_above_zero_are_valid_seconds(typed: str | float) -> None:
    assert check_settings(raw(quiet_seconds=typed, max_wait_seconds="60")).ok


def test_empty_is_an_error_never_zero() -> None:
    errors = check_settings(raw(quiet_seconds="", max_wait_seconds="")).errors
    assert errors == {"quiet_seconds": NUMBER, "max_wait_seconds": NUMBER}


@pytest.mark.parametrize(
    ("quiet", "typed_n"),
    [("10", "10"), ("2.50", "2.50"), (" 7 ", "7"), (10, "10"), (2.5, "2.5"), (10.0, "10")],
)
def test_max_wait_below_quiet_names_the_quiet_time_as_typed(
    quiet: str | float, typed_n: str
) -> None:
    errors = check_settings(raw(quiet_seconds=quiet, max_wait_seconds="1")).errors
    assert errors == {"max_wait_seconds": f"Must be at least the quiet time ({typed_n} seconds)."}


def test_max_wait_equal_to_quiet_is_valid() -> None:
    assert check_settings(raw(quiet_seconds="2.5", max_wait_seconds="2.5")).ok


def test_an_invalid_quiet_time_is_not_compared() -> None:
    errors = check_settings(raw(quiet_seconds="x", max_wait_seconds="1")).errors
    assert errors == {"quiet_seconds": NUMBER}


@pytest.mark.parametrize("typed", ["", "abc", "-1", "201", "20.5", "1e2", 201, -1])
def test_diff_lines_must_be_0_to_200(typed: str | int) -> None:
    assert check_settings(raw(max_lines=typed)).errors == {"max_lines": LINES}


@pytest.mark.parametrize("typed", ["0", "200", " 20 ", 0, 200])
def test_diff_lines_bounds_are_valid(typed: str | int) -> None:
    assert check_settings(raw(max_lines=typed)).ok


@pytest.mark.parametrize(
    ("window", "errors"),
    [
        ("23:00-08:00", {}),
        ("-08:00", {"quiet_from": TIME}),
        ("23:00-", {"quiet_to": TIME}),
        ("", {"quiet_from": TIME, "quiet_to": TIME}),
        ("25:00-08:00", {"quiet_from": TIME}),
        ("23:00-8:00", {"quiet_to": TIME}),
        ("23:00-08:00-09:00", {"quiet_to": TIME}),
        ("08:00-08:00", {"quiet_to": DIFFERS}),
    ],
)
def test_quiet_hours_errors_name_the_from_or_to_field(window: str, errors: dict[str, str]) -> None:
    assert check_settings(raw(quiet_hours=window)).errors == errors


@pytest.mark.parametrize(
    "window",
    ["23:00-08:00", "00:00-23:59", "08:00-08:00", "-08:00", "24:00-01:00", "", "a-b", "9:00-10:00"],
)
def test_the_quiet_hours_rule_is_the_config_models(window: str) -> None:
    try:
        NotifyConfig(quiet_hours=window)
        model_ok = True
    except ValidationError:
        model_ok = False
    assert check_settings(raw(quiet_hours=window)).ok is model_ok


@pytest.mark.parametrize("bad", ["9:00", "24:00", "noon", "", "09:60"])
def test_the_heartbeat_needs_a_time(bad: str) -> None:
    assert check_settings(raw(heartbeat=bad)).errors == {"heartbeat": TIME}


def test_off_switches_are_valid() -> None:
    assert check_settings(raw(quiet_hours=None, heartbeat=None)).ok


def test_the_number_bounds_are_the_config_models() -> None:
    DiffConfig(max_lines=200)
    DebounceConfig(quiet_seconds=2.5, max_wait_seconds=2.5)
    for bad in (lambda: DiffConfig(max_lines=201), lambda: DebounceConfig(quiet_seconds=0)):
        with pytest.raises(ValidationError):
            bad()


def test_pattern_issues_use_check_pattern_without_its_prefix() -> None:
    result = check_settings(
        raw(
            defaults=["ok/*.json", "/abs"],
            redact=["*.secret", "it's[", "a//b"],
        )
    )
    assert result.errors == {}
    assert result.issues == {
        "defaults": [Issue(2, "patterns are relative to the project root")],
        "redact": [Issue(2, "unclosed '['"), Issue(3, "empty path segment")],
    }
    assert result.body()["issues"]["redact"][0] == {"line": 2, "reason": "unclosed '['"}


def test_an_unsupported_class_keeps_check_patterns_reason() -> None:
    [issue] = check_settings(raw(ignore=["[z-a]"])).issues["ignore"]
    assert issue.line == 1 and issue.reason.startswith("unsupported character class (")


def test_the_summary_is_one_line_of_at_most_five_problems() -> None:
    result = check_settings(
        raw(
            quiet_seconds="",
            max_lines="999",
            heartbeat="x",
            redact=["/a", "/b", "/c"],
        )
    )
    assert result.summary() == (
        "quiet_seconds: Enter a number of seconds.; max_lines: Enter 0 to 200 lines.; "
        "heartbeat: Use a time like 23:00.; "
        "redact line 1: patterns are relative to the project root; "
        "redact line 2: patterns are relative to the project root …and 1 more"
    )


def test_settings_values_parses_a_checked_body() -> None:
    assert settings_values(raw(quiet_seconds=" 2.5 ", max_wait_seconds=30, max_lines="40")) == (
        2.5,
        30.0,
        40,
    )
    with pytest.raises(ValueError):
        settings_values(raw(quiet_seconds=""))


def model_errors(data: dict[str, Any]) -> list[Any]:
    with pytest.raises(ValidationError) as caught:
        Config.model_validate(data)
    return list(caught.value.errors())


@pytest.mark.parametrize(
    ("data", "worded"),
    [
        (
            {"debounce": {"quiet_seconds": 10, "max_wait_seconds": 5}},
            ("debounce.max_wait_seconds", "Must be at least the quiet time (10 seconds)."),
        ),
        (
            {"debounce": {"quiet_seconds": 2.5, "max_wait_seconds": 1}},
            ("debounce.max_wait_seconds", "Must be at least the quiet time (2.5 seconds)."),
        ),
        ({"debounce": {"quiet_seconds": 0}}, ("debounce.quiet_seconds", POSITIVE)),
        ({"debounce": {"max_wait_seconds": "MARKER"}}, ("debounce.max_wait_seconds", NUMBER)),
        ({"diff": {"max_lines": 500}}, ("diff.max_lines", LINES)),
        ({"notify": {"heartbeat": "9"}}, ("notify.heartbeat", TIME)),
        ({"notify": {"quiet_hours": "x"}}, ("notify.quiet_hours", TIME)),
        ({"notify": {"quiet_hours": "08:00-08:00"}}, ("notify.quiet_hours", DIFFERS)),
        ({"notify": {"machine": "x" * 41}}, ("notify.machine", MACHINE)),
        ({"notify": {"machine": "a\u200bb"}}, ("notify.machine", MACHINE)),
    ],
)
def test_message_for_words_the_config_models_errors(
    data: dict[str, Any], worded: tuple[str, str]
) -> None:
    [error] = model_errors(data)
    assert message_for(error) == worded


def test_message_for_leaves_other_errors_alone() -> None:
    [error] = model_errors({"web": {"port": 1}})
    assert message_for(error) is None


@pytest.mark.parametrize("typed", [5.5, True, False, "5.5", float("nan"), float("inf")])
def test_diff_lines_reject_fractions_bools_and_non_numbers(typed: Any) -> None:
    assert check_settings(raw(max_lines=typed)).errors == {"max_lines": LINES}


def test_a_whole_float_is_valid_diff_lines() -> None:
    body = raw(max_lines=20.0)
    assert check_settings(body).ok
    assert settings_values(body)[2] == 20
    assert isinstance(settings_values(body)[2], int)


@pytest.mark.parametrize("typed", [True, False])
def test_a_bool_is_not_a_number_of_seconds(typed: bool) -> None:
    assert check_settings(raw(quiet_seconds=typed)).errors == {"quiet_seconds": NUMBER}


def test_the_summary_says_how_many_more_problems_there_are() -> None:
    result = check_settings(raw(quiet_seconds="", max_lines="999", redact=["/a"] * 6))
    assert result.summary().endswith(
        "; redact line 3: patterns are relative to the project root …and 3 more"
    )
    assert check_settings(raw(redact=["/a"] * 5)).summary().count("more") == 0


@pytest.mark.parametrize("typed", [None, [], [10], {}, {"a": 1}, object()])
def test_a_non_number_type_is_the_seconds_message(typed: Any) -> None:
    result = check_settings(raw(quiet_seconds=typed, max_wait_seconds=typed))
    assert result.errors == {"quiet_seconds": NUMBER, "max_wait_seconds": NUMBER}


@pytest.mark.parametrize("typed", [None, [], [20], {}, {"a": 1}, object()])
def test_a_non_number_type_is_the_lines_message(typed: Any) -> None:
    assert check_settings(raw(max_lines=typed)).errors == {"max_lines": LINES}


@pytest.mark.parametrize("typed", [None, [10]])
def test_settings_values_refuses_a_non_number_type(typed: Any) -> None:
    with pytest.raises(ValueError, match="passed check_settings"):
        settings_values(raw(quiet_seconds=typed))
    with pytest.raises(ValueError, match="passed check_settings"):
        settings_values(raw(max_lines=typed))


# -- notify.machine (machine name spec §2) ------------------------------------------------


@pytest.mark.parametrize("bad", ["x" * 41, "Work\nMBP", "a\u202eb", "a\x00"])
def test_the_machine_name_has_one_rule_and_message(bad: str) -> None:
    assert check_settings(raw(machine=bad)).errors == {"machine": MACHINE}
    with pytest.raises(ValidationError):
        NotifyConfig(machine=bad)


@pytest.mark.parametrize("good", [None, "", "Work MBP", f"  {'x' * 40} ", "Felix’s Mac"])
def test_a_valid_machine_name_or_none_passes(good: str | None) -> None:
    assert check_settings(raw(machine=good)).ok
    NotifyConfig(machine=good)
