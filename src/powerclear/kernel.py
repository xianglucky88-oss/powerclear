"""Clearing-price normalisation rules.

Reliability contract
--------------------
* Legitimate negative prices, true zeros, missing values and very large
  fills are distinct outcomes.  They are never collapsed into one
  anomaly bucket and never silently coerced to ``0``.
* Large magnitudes are kept exactly as reported.  ``scale_factor`` only
  *describes* the observed unit scale (e.g. a source that switched a
  node from MWh to kWh); it is never applied to the value, so a
  unit-scale adjustment cannot be misread as a market price jump.
* Every record carries its raw expression, the observed precision, the
  reason codes behind each decision and the rule version, so historical
  judgements remain explainable after the rules change.

``process`` is pure and deterministic: re-processing the same record,
in any arrival order, yields the same judgement.
"""
from __future__ import annotations

import math
import re

#: Bump whenever the rules below change.  Stamped on every processed
#: record so stored judgements keep their provenance.
RULE_VERSION = "price-rules/2"

#: Magnitude at or above which a fill is flagged as large.  The flag is
#: informational only; the value itself is left untouched.
LARGE_MAGNITUDE = 1_000_000.0

#: Upper bound for the descriptive scale factor (guards absurd inputs
#: such as 1e300 without looping forever).
_MAX_SCALE = 10 ** 15

_MISSING_TOKENS = frozenset(
    {"", "-", "--", "n/a", "na", "null", "none", "missing"})
_NON_FINITE_TOKENS = frozenset(
    {"nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"})
_GROUPED_RE = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?([eE][+-]?\d+)?$")
_PLAIN_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def _decimals_in(text):
    """Decimal places visible in the mantissa of a numeric expression."""
    mantissa = re.split("[eE]", text, maxsplit=1)[0]
    return len(mantissa.split(".", 1)[1]) if "." in mantissa else 0


def _json_safe_raw(raw):
    """Keep the raw expression JSON-serialisable for storage."""
    if isinstance(raw, float) and not math.isfinite(raw):
        return repr(raw)
    return raw


def _parse(raw):
    """Return ``(value, status, flags, reasons, precision)`` for *raw*.

    ``status`` is one of ``"ok"``, ``"missing"`` or ``"invalid"``;
    the three are never conflated and never mapped to ``0``.
    """
    if raw is None:
        return None, "missing", [], ["value_absent"], None
    if isinstance(raw, bool):
        return None, "invalid", [], ["boolean_is_not_a_price"], None
    if isinstance(raw, int):
        return float(raw), "ok", [], [], 0
    if isinstance(raw, float):
        if not math.isfinite(raw):
            return None, "invalid", [], ["non_finite_value"], None
        return raw, "ok", [], [], _decimals_in(repr(raw))
    if isinstance(raw, str):
        text = raw.strip()
        low = text.lower()
        if low in _MISSING_TOKENS:
            return None, "missing", [], ["blank_or_na_token"], None
        if low in _NON_FINITE_TOKENS:
            return None, "invalid", [], ["non_finite_literal"], None
        flags, reasons = [], []
        cleaned = text
        if _GROUPED_RE.match(text):
            cleaned = text.replace(",", "")
            flags.append("grouped_digits")
            reasons.append("thousand_separators_removed")
        if not _PLAIN_RE.match(cleaned):
            return None, "invalid", [], ["unparseable_expression"], None
        value = float(cleaned)
        if not math.isfinite(value):
            return None, "invalid", [], ["non_finite_after_parse"], None
        if "e" in cleaned.lower():
            flags.append("scientific_notation")
            reasons.append("parsed_scientific_notation")
        return value, "ok", flags, reasons, _decimals_in(cleaned)
    return None, "invalid", [], ["unsupported_type:%s" % type(raw).__name__], None


def process(record):
    """Classify and normalise the price field of *record*.

    Returns a dict of derived fields merged into the record by the
    caller.  Never raises on bad input; outlier and malformed inputs
    are flagged, not crashed on.
    """
    raw = record.get("value", None)
    value, status, flags, reasons, precision = _parse(raw)

    norm = None
    scale = 1
    was_large = 0
    if status == "ok":
        if abs(value) >= LARGE_MAGNITUDE:
            was_large = 1
            while abs(value) / scale >= LARGE_MAGNITUDE and scale < _MAX_SCALE:
                scale *= 1000
            flags.append("large_magnitude")
            flags.append("unit_scale_suspected")
            reasons.append("large_fill_kept_not_rescaled")
            reasons.append("possible_unit_scale_not_a_price_jump")
        if value == 0:
            flags.append("true_zero")
            reasons.append("true_zero_not_missing")
        elif value < 0:
            flags.append("negative_price")
            reasons.append("negative_price_is_legitimate")
        norm = round(value, 4)
        if norm != value:
            reasons.append("rounded_to_4dp")

    return {
        "value_raw": _json_safe_raw(raw),
        "value_norm": norm,
        "value_status": status,
        "value_precision": precision,
        "scale_factor": scale,
        "was_large": was_large,
        "flags": sorted(set(flags)),
        "reasons": list(reasons),
        "rule_version": RULE_VERSION,
    }
