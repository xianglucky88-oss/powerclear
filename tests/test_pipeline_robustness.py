"""End-to-end reliability: flaky transport, bad records, reruns, recovery."""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from powerclear import framework, kernel
from powerclear.framework import Http, Sleeper, Transport
from powerclear.store import Store

ROOT = Path(__file__).resolve().parents[1]
TS0 = datetime(2030, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #
def mem_store():
    con = sqlite3.connect(":memory:")
    return con, Store(con)


def make_rows(n, start=1, value_fn=None, ts_fn=None):
    rows = []
    for i in range(start, start + n):
        rows.append({
            "id": "R-%04d" % i,
            "name": "item-%d" % (i % 3),
            "value": value_fn(i) if value_fn else 100 + i,
            "status": "active",
            "ts": (ts_fn(i) if ts_fn else TS0 + timedelta(days=i)
                   ).isoformat().replace("+00:00", "Z"),
            "node": "node_%d" % (i % 5),
        })
    return rows


def pages_of(rows, size=4):
    return [rows[i:i + size] for i in range(0, len(rows), size)]


class ScriptedTransport:
    """Transport driven by a handler function; records every call."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, path, params=None, json_body=None, headers=None):
        self.calls.append({"method": method, "path": path,
                           "params": dict(params or {})})
        return self.handler(method, path, dict(params or {}))


def paged_handler(pages, faults=None):
    """Serve ``pages`` (1-based); ``faults`` maps page -> scripted statuses."""
    faults = faults or {}
    seen = {}

    def handler(method, path, params):
        page = int(params.get("page", 1))
        idx = seen.get(page, 0)
        seen[page] = idx + 1
        scripted = faults.get(page, [])
        if idx < len(scripted):
            return scripted[idx], {}, ""
        rows = pages[page - 1] if 0 < page <= len(pages) else []
        return 200, {}, json.dumps({"items": rows})

    return handler


def run_with(pages, mode="full", con=None, faults=None):
    http = Http(ScriptedTransport(paged_handler(pages, faults)), Sleeper())
    return framework.run(mode=mode, con=con, http=http), http


def snapshot(store):
    return sorted(json.dumps(r, sort_keys=True, ensure_ascii=False)
                  for r in store.all_records())


# --------------------------------------------------------------------------- #
# Rate limiting (限流).
# --------------------------------------------------------------------------- #
def test_rate_limit_retries_then_succeeds():
    con, store = mem_store()
    pages = pages_of(make_rows(9))
    result, http = run_with(pages, con=con, faults={1: [429, 429]})
    assert result["count"] == 9
    assert result["http_attempts"] == 5  # 3 attempts on page 1, 1 each after
    # politeness on every attempt plus exponential backoff on the two 429s
    assert result["slept"] == pytest.approx(0.25 * 5 + 0.5 + 1.0)
    assert http.sleeper.calls > 5


def test_rate_limit_exhausted_fails_run_without_touching_store():
    con, store = mem_store()
    with pytest.raises(RuntimeError):
        run_with(pages_of(make_rows(9)), con=con, faults={1: [429] * 6})
    assert store.count() == 0
    assert store.watermark() is None


def test_bundled_fixture_rate_limit_endpoint_recovers():
    http = Http(Transport(), Sleeper())
    status, _headers, body, _attempts = http.get("/probe")
    assert status == 200
    assert json.loads(body)["ok"] is True


# --------------------------------------------------------------------------- #
# Duplicate pages and re-received content (重复页 / 重复接收).
# --------------------------------------------------------------------------- #
def test_duplicate_pages_do_not_duplicate_records():
    con, store = mem_store()
    rows = make_rows(6)
    pages = [rows[0:4], rows[0:4], rows[4:6]]  # page 1 delivered twice
    result, _ = run_with(pages, con=con)
    assert result["fetched"] == 10
    assert result["duplicates"] == 4
    assert result["count"] == 6


def test_full_rerun_is_idempotent():
    con, store = mem_store()
    pages = pages_of(make_rows(9))
    first, _ = run_with(pages, con=con)
    snap = snapshot(store)
    second, _ = run_with(pages, con=con)
    assert first["count"] == second["count"] == 9
    assert second["ingested"] == 0
    assert snapshot(store) == snap


# --------------------------------------------------------------------------- #
# Out-of-order arrival and late reports (乱序补报).
# --------------------------------------------------------------------------- #
def test_page_arrival_order_does_not_change_results():
    rows = make_rows(9)
    pages = pages_of(rows)
    con_a, store_a = mem_store()
    con_b, store_b = mem_store()
    run_with(pages, con=con_a)
    run_with([pages[1], pages[0], pages[2]], con=con_b)  # swapped order
    assert snapshot(store_a) == snapshot(store_b)
    assert store_a.watermark() == store_b.watermark()


def test_late_out_of_order_report_lands_exactly_once_on_full_rerun():
    con, store = mem_store()
    rows = make_rows(9)
    run_with(pages_of(rows), con=con)

    late_old_ts = make_rows(1, start=10,
                            ts_fn=lambda i: TS0 + timedelta(days=2))[0]
    new_row = make_rows(1, start=11,
                        ts_fn=lambda i: TS0 + timedelta(days=40))[0]
    result, _ = run_with(pages_of(rows + [late_old_ts, new_row]), con=con)
    assert result["count"] == 11
    ids = [r["id"] for r in store.all_records()]
    assert ids.count("R-0010") == 1 and ids.count("R-0011") == 1

    again, _ = run_with(pages_of(rows + [late_old_ts, new_row]), con=con)
    assert again["count"] == 11 and again["ingested"] == 0


# --------------------------------------------------------------------------- #
# Partial failure and incremental recovery (局部失败 / 增量恢复).
# --------------------------------------------------------------------------- #
def test_partial_failure_writes_nothing_and_recovers_afterwards():
    con, store = mem_store()
    pages = pages_of(make_rows(9))
    with pytest.raises(RuntimeError):
        run_with(pages, con=con, faults={2: [500] * 5})
    assert store.count() == 0
    assert store.watermark() is None

    recovered, _ = run_with(pages, con=con)  # source healthy again
    assert recovered["count"] == 9
    assert store.watermark() is not None


def test_partial_failure_preserves_previously_stored_data():
    con, store = mem_store()
    good, _ = run_with(pages_of(make_rows(9)), con=con)
    wm = store.watermark()
    grown = pages_of(make_rows(11))
    with pytest.raises(RuntimeError):
        run_with(grown, con=con, faults={3: [503] * 5})
    assert store.count() == good["count"] == 9
    assert store.watermark() == wm


def test_incremental_run_recovers_new_rows_only():
    con, store = mem_store()
    rows = make_rows(9)
    run_with(pages_of(rows), con=con)
    new_row = make_rows(1, start=11,
                        ts_fn=lambda i: TS0 + timedelta(days=40))[0]
    inc, _ = run_with(pages_of(rows + [new_row]), mode="incremental", con=con)
    assert inc["mode"] == "incremental"
    assert inc["ingested"] == 1
    assert inc["count"] == 10
    again, _ = run_with(pages_of(rows + [new_row]), mode="incremental", con=con)
    assert again["ingested"] == 0
    assert again["count"] == 10


# --------------------------------------------------------------------------- #
# Bad records must not affect normal clearing data (坏记录隔离).
# --------------------------------------------------------------------------- #
def test_bad_records_do_not_block_or_corrupt_good_ones():
    con, store = mem_store()
    rows = make_rows(3)  # good rows, values 101, 102, 103
    bad = [
        {"id": "B-1", "name": "x", "value": "abc", "status": "active",
         "ts": "2030-02-01T00:00:00Z", "node": "node_1"},
        {"id": "B-2", "name": "x", "value": "", "status": "active",
         "ts": "2030-02-02T00:00:00Z", "node": "node_1"},
        {"id": "B-3", "name": "x", "value": "nan", "status": "active",
         "ts": "2030-02-03T00:00:00Z", "node": "node_1"},
        {"id": "B-4", "name": "x", "value": None, "status": "active",
         "ts": "2030-02-04T00:00:00Z", "node": "node_1"},
    ]
    result, _ = run_with(pages_of(rows + bad), con=con)
    assert result["count"] == 7  # bad rows are quarantined, not fatal

    by_id = {r["id"]: r for r in store.all_records()}
    assert by_id["R-0001"]["value_norm"] == 101.0
    assert by_id["R-0001"]["value_status"] == "ok"
    assert by_id["B-1"]["value_status"] == "invalid"
    assert by_id["B-1"]["value_norm"] is None
    assert by_id["B-2"]["value_status"] == "missing"
    assert by_id["B-3"]["value_status"] == "invalid"
    assert by_id["B-4"]["value_status"] == "missing"


def test_kernel_failure_on_one_record_is_isolated(monkeypatch):
    con, store = mem_store()
    real_process = kernel.process

    def flaky(record):
        if record.get("id") == "R-0002":
            raise ValueError("boom")
        return real_process(record)

    monkeypatch.setattr(kernel, "process", flaky)
    result, _ = run_with(pages_of(make_rows(4)), con=con)
    assert result["count"] == 4
    by_id = {r["id"]: r for r in store.all_records()}
    assert by_id["R-0002"]["value_status"] == "invalid"
    assert "kernel_error" in by_id["R-0002"]["flags"]
    assert by_id["R-0001"]["value_status"] == "ok"
    assert by_id["R-0001"]["value_norm"] == 101.0


# --------------------------------------------------------------------------- #
# Provenance across rule changes (规则变化 / 历史判断出处).
# --------------------------------------------------------------------------- #
def test_rule_version_is_recorded_per_run_and_per_record():
    con, store = mem_store()
    result, _ = run_with(pages_of(make_rows(4)), con=con)
    assert result["rule_version"] == kernel.RULE_VERSION
    assert json.loads(store.get_meta("rule_versions")) == [kernel.RULE_VERSION]
    for record in store.all_records():
        assert record["rule_version"] == kernel.RULE_VERSION


def test_rule_change_keeps_historical_judgements_traceable(monkeypatch):
    con, store = mem_store()
    pages = pages_of(make_rows(4))
    run_with(pages, con=con)
    old_version = kernel.RULE_VERSION

    monkeypatch.setattr(kernel, "RULE_VERSION", "price-rules/2.1")
    run_with(pages, con=con)

    assert json.loads(store.get_meta("rule_versions")) == [
        old_version, "price-rules/2.1"]
    # Historical rows are never rewritten: their original stamp survives.
    historical = [r for r in store.all_records()
                  if r["rule_version"] == old_version]
    assert len(historical) == 4
    assert all("value_norm" in r for r in historical)


def test_legacy_rows_without_rule_version_remain_usable():
    con, store = mem_store()
    legacy = {"id": "LEG-1", "name": "n", "value": 5, "status": "active",
              "ts": "2030-01-01T00:00:00Z", "node": "node_1",
              "value_norm": 5.0, "scale_factor": 1, "was_large": 0}
    assert store.upsert(legacy) is True

    result, _ = run_with(pages_of(make_rows(4)), con=con)
    assert result["count"] == 5
    assert store.get(Store.hash(legacy)) == legacy  # untouched
    assert legacy in store.all_records()


# --------------------------------------------------------------------------- #
# Offline entry points (现有离线运行入口).
# --------------------------------------------------------------------------- #
def _cli_env(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = "%s:%s" % (ROOT / "src", ROOT)
    env["POWERCLEAR_DB"] = str(tmp_path / "cli.db")
    return env


def _cli(args, env):
    return subprocess.run(
        [sys.executable, "-m", "powerclear.cli"] + args,
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=120)


def test_cli_full_and_incremental_entry_points(tmp_path):
    env = _cli_env(tmp_path)

    first = _cli(["--full"], env)
    assert first.returncode == 0, first.stderr
    out = json.loads(first.stdout)
    assert out["engine"] == "crawl" and out["mode"] == "full"
    assert out["fetched"] == 10 and out["duplicates"] == 1
    assert out["ingested"] == 9 and out["count"] == 9
    assert out["rule_version"] == kernel.RULE_VERSION

    inc = _cli(["--incremental"], env)
    assert inc.returncode == 0, inc.stderr
    out_inc = json.loads(inc.stdout)
    assert out_inc["mode"] == "incremental"
    assert out_inc["ingested"] == 0 and out_inc["count"] == 9

    rerun = _cli(["--full"], env)
    assert rerun.returncode == 0, rerun.stderr
    out_re = json.loads(rerun.stdout)
    assert out_re["ingested"] == 0 and out_re["count"] == 9

    # Historical data written by the CLI stays readable afterwards.
    con = sqlite3.connect(str(tmp_path / "cli.db"))
    try:
        store = Store(con)
        assert store.count() == 9
        assert store.watermark()
        assert kernel.RULE_VERSION in json.loads(store.get_meta("rule_versions"))
    finally:
        con.close()
