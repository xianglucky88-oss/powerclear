"""Kernel reliability: distinct outcomes for distinct price situations."""
import json
import math

import pytest

from powerclear import kernel


def proc(value):
    return kernel.process({"id": "X", "value": value, "ts": "t"})


def test_legitimate_negative_price_is_kept_and_flagged():
    out = proc("-12.5")
    assert out["value_status"] == "ok"
    assert out["value_norm"] == -12.5
    assert "negative_price" in out["flags"]
    assert "invalid" not in out["flags"]


def test_true_zero_is_not_missing():
    zero = proc(0)
    assert zero["value_status"] == "ok"
    assert zero["value_norm"] == 0.0
    assert "true_zero" in zero["flags"]

    for raw in (None, "", "   ", "N/A", "null", "-"):
        missing = proc(raw)
        assert missing["value_status"] == "missing", raw
        assert missing["value_norm"] is None
        assert missing["value_norm"] != 0  # never coerced to a fake zero
        assert "true_zero" not in missing["flags"]


def test_missing_and_invalid_are_distinct_classes():
    assert proc("")["value_status"] == "missing"
    assert proc("abc")["value_status"] == "invalid"
    assert proc("")["reasons"] != proc("abc")["reasons"]


def test_large_fill_is_kept_not_rescaled():
    out = proc(2500000)
    assert out["value_norm"] == 2500000.0  # never divided down
    assert out["was_large"] == 1
    assert out["scale_factor"] == 1000  # descriptive only, not applied
    assert "large_magnitude" in out["flags"]
    assert "unit_scale_suspected" in out["flags"]
    assert "large_fill_kept_not_rescaled" in out["reasons"]


def test_large_negative_fill_gets_both_flags_not_one_anomaly_bucket():
    out = proc(-2_500_000)
    assert out["value_status"] == "ok"
    assert out["value_norm"] == -2_500_000.0
    assert "negative_price" in out["flags"]
    assert "large_magnitude" in out["flags"]


@pytest.mark.parametrize("raw,expected", [
    ("1.25e3", 1250.0),
    ("2E6", 2000000.0),
    ("1.5e-2", 0.015),
    ("+3e0", 3.0),
])
def test_scientific_notation_is_parsed_and_explained(raw, expected):
    out = proc(raw)
    assert out["value_status"] == "ok"
    assert out["value_norm"] == pytest.approx(expected)
    assert "scientific_notation" in out["flags"]
    assert "parsed_scientific_notation" in out["reasons"]


@pytest.mark.parametrize("raw", [
    "nan", "inf", "-inf", "Infinity", "1e400", "abc", "12.34.56",
    "1,2,3", "0x10", "e5", "",  # empty is missing, still not a crash
    True, False, {"v": 1}, [1, 2],
    float("nan"), float("inf"), float("-inf"),
])
def test_outlier_and_invalid_inputs_never_crash_and_never_become_zero(raw):
    out = proc(raw)
    assert out["value_status"] in ("missing", "invalid")
    assert out["value_norm"] is None
    assert out["reasons"]  # every judgement carries a reason
    # the whole derived record must stay JSON-serialisable for storage
    json.dumps(out, sort_keys=True)


def test_non_finite_float_raw_expression_is_stored_as_text():
    out = proc(float("nan"))
    assert out["value_raw"] == "nan"
    json.dumps(out)


def test_grouped_digits_are_accepted_and_explained():
    out = proc("1,234.5")
    assert out["value_norm"] == 1234.5
    assert "grouped_digits" in out["flags"]
    assert "thousand_separators_removed" in out["reasons"]


def test_precision_difference_is_explainable():
    precise = proc("12.345678")
    assert precise["value_precision"] == 6
    assert precise["value_norm"] == round(12.345678, 4)
    assert "rounded_to_4dp" in precise["reasons"]

    exact = proc("12.34")
    assert exact["value_precision"] == 2
    assert exact["value_norm"] == 12.34
    assert "rounded_to_4dp" not in exact["reasons"]

    assert proc(7)["value_precision"] == 0


def test_raw_expression_is_preserved():
    assert proc("  1.25e3 ")["value_raw"] == "  1.25e3 "
    assert proc(2500000)["value_raw"] == 2500000
    assert proc(None)["value_raw"] is None


def test_rule_version_is_stamped_for_provenance():
    out = proc(1)
    assert out["rule_version"] == kernel.RULE_VERSION


def test_process_is_pure_and_deterministic():
    record = {"id": "X", "value": "1.25e3", "ts": "t"}
    first = kernel.process(dict(record))
    second = kernel.process(dict(record))
    assert first == second
    assert record == {"id": "X", "value": "1.25e3", "ts": "t"}  # no mutation


def test_every_possible_status_is_reachable_and_finite_output():
    statuses = {proc(v)["value_status"]
                for v in (None, "abc", 1, -1, 0, 1e7, "1e3")}
    assert statuses == {"missing", "invalid", "ok"}
    for v in (None, "abc", 1, -1, 0, 1e7, "1e3", 1e300):
        out = proc(v)
        assert out["value_norm"] is None or math.isfinite(out["value_norm"])
