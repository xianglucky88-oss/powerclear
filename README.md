# powerclear

A small offline data-processing service with a local HTTP fixture and SQLite storage. It is self-contained and does not require network access.

## Usage

```text
make run       # process the bundled source
make run_inc   # process using the incremental command-line mode
make test      # run the test suite
make mock      # start the local fixture service
```

The package exposes a Python entry point under `src/powerclear`. The fixture service supports the source formats used by the package, and the command-line entry point writes results to the local SQLite database. Re-running commands is safe for an existing database.

Set `POWERCLEAR_DB` to point a run at a different database file (the default is `powerclear.db` in the repository root).

## Price reliability contract

Clearing prices are classified, never silently coerced. Each record is stamped with derived fields from `powerclear.kernel`:

- `value_raw` — the original expression as received (non-finite floats are stored as text so payloads stay valid JSON).
- `value_status` — `ok`, `missing` or `invalid`. Missing (`""`, `N/A`, absent) and invalid (`abc`, `nan`, `1e400`, …) are distinct classes and are never mapped to `0`.
- `value_norm` — the parsed value rounded to 4 decimals, or `null` when not `ok`.
- `value_precision` — decimal places observed in the raw expression; `rounded_to_4dp` is added to `reasons` when rounding actually changed the value.
- `flags` — e.g. `negative_price` (legitimate, kept), `true_zero` (distinct from missing), `large_magnitude` / `unit_scale_suspected` (large fills are kept as reported; the scale is descriptive and never applied, so a unit-scale adjustment at a node is not misread as a price jump), `scientific_notation`, `grouped_digits`.
- `reasons` — machine-readable justification for every decision.
- `rule_version` — the rule set that produced the judgement. Records are never rewritten (`INSERT OR IGNORE`), so historical judgements keep their stamp when rules change; every version ever applied is listed in the `rule_versions` store metadata.

Processing is pure and deterministic: duplicate deliveries and arrival order do not change the stored result for a period, and one bad record cannot affect the normal records around it (it is quarantined with an `invalid` status). Reliability scenarios — rate limiting, duplicate pages, out-of-order late reports, partial failures, full reruns and incremental recovery — are covered by `tests/test_pipeline_robustness.py`.
