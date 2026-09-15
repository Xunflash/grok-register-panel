# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
import tempfile
from datetime import timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime_platform import now_beijing
from success_stop import (  # noqa: E402
    normalize_success_stop,
    should_stop_from_hour_window,
    should_stop_low_success,
    success_rate_percent,
    window_success_counts,
)
from webui.api_projection import summary


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_does_not_stop_until_attempts_exceed_floor():
    settings = {"stop_min_accounts": 10, "stop_min_success_rate": 65}
    stop, _reason = should_stop_low_success(0, 10, settings)
    assert stop is False
    stop, reason = should_stop_low_success(3, 8, settings)
    assert stop is True
    assert "11" in reason
    assert "27.3%" in reason
    assert "近1小时" in reason


def test_exactly_threshold_rate_continues():
    settings = {"stop_min_accounts": 10, "stop_min_success_rate": 65}
    assert should_stop_low_success(13, 7, settings)[0] is False
    assert should_stop_low_success(12, 8, settings)[0] is True


def test_zero_rate_floor_never_stops():
    settings = {"stop_min_accounts": 1, "stop_min_success_rate": 0}
    assert should_stop_low_success(0, 20, settings)[0] is False


def test_normalize_clamps_and_defaults():
    assert normalize_success_stop({}) == {
        "stop_min_accounts": 10,
        "stop_min_success_rate": 65.0,
    }
    out = normalize_success_stop({"stop_min_accounts": "99999", "stop_min_success_rate": "-3"})
    assert out["stop_min_accounts"] == 5000
    assert out["stop_min_success_rate"] == 0.0
    assert success_rate_percent(2, 2) == 50.0


def test_save_control_persists_success_stop():
    from webui import monitor

    with tempfile.TemporaryDirectory() as temp:
        control_file = Path(temp) / "monitor_control.json"
        previous = monitor.CONTROL_FILE
        monitor.CONTROL_FILE = control_file
        try:
            saved = monitor.save_control(
                {
                    "stop_min_accounts": 12,
                    "stop_min_success_rate": 70,
                    "workers": 2,
                }
            )
        finally:
            monitor.CONTROL_FILE = previous
        assert saved["stop_min_accounts"] == 12
        assert saved["stop_min_success_rate"] == 70.0
        disk = json.loads(control_file.read_text(encoding="utf-8"))
        assert disk["stop_min_accounts"] == 12
        assert disk["stop_min_success_rate"] == 70.0


def test_save_control_persists_debug_mode_har_flag():
    from webui import monitor

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        control_file = root / "monitor_control.json"
        config_file = root / "config.json"
        log_dir = root / "log"
        log_dir.mkdir()
        config_file.write_text(json.dumps({"debug_mode": False, "turnstile_provider": "capsolver"}), encoding="utf-8")
        previous = monitor.CONTROL_FILE, monitor.ROOT, monitor.LOG_DIR
        monitor.CONTROL_FILE = control_file
        monitor.ROOT = root
        monitor.LOG_DIR = log_dir
        try:
            saved = monitor.save_control({"debug_mode": True, "workers": 2})
            env_on = monitor._registration_env()
            cfg_on = json.loads(config_file.read_text(encoding="utf-8"))
            saved_off = monitor.save_control({"debug_mode": False})
            env_off = monitor._registration_env()
            cfg_off = json.loads(config_file.read_text(encoding="utf-8"))
        finally:
            monitor.CONTROL_FILE, monitor.ROOT, monitor.LOG_DIR = previous
        assert saved["debug_mode"] is True
        assert cfg_on["debug_mode"] is True
        assert saved_off["debug_mode"] is False
        assert cfg_off["debug_mode"] is False
        assert env_on.get("GROK_DEBUG_MODE") == "1"
        assert env_on.get("GROK_RECORD_HAR_DIR") == str(log_dir / "browser-hars")
        assert (log_dir / "browser-hars").is_dir()
        assert env_off.get("GROK_DEBUG_MODE") in (None, "")
        assert "GROK_RECORD_HAR_DIR" not in env_off


def test_hour_window_matches_panel_success_stats():
    from webui import monitor

    now = now_beijing()
    recent = (now - timedelta(minutes=10)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (now - timedelta(hours=3)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = (
        [{"ts": recent, "status": "ok"}] * 8
        + [{"ts": recent, "status": "fail"}] * 4
        + [{"ts": recent, "status": "risk"}] * 2
        + [{"ts": old, "status": "ok"}] * 20
        + [{"ts": old, "status": "fail"}] * 20
    )
    with tempfile.TemporaryDirectory() as temp:
        log_dir = Path(temp)
        jsonl = log_dir / "register_results.jsonl"
        _write_jsonl(jsonl, rows)
        counts = window_success_counts(jsonl, hours=1, now=now, cache_seconds=0)
        assert counts["ok"] == 8
        assert counts["fail"] == 4
        assert counts["risk"] == 2
        assert counts["total"] == 14
        assert counts["success_rate"] == 57.1

        prev_log = monitor.LOG_DIR
        prev_cache = monitor.STATS_CACHE
        monitor.LOG_DIR = log_dir
        monitor.STATS_CACHE = log_dir / "monitor_stats.json"
        try:
            stats = monitor.success_stats()
        finally:
            monitor.LOG_DIR = prev_log
            monitor.STATS_CACHE = prev_cache
        hour = stats["rates"]["1h"]
        assert hour["ok"] == counts["ok"]
        assert hour["fail"] == counts["fail"]
        assert hour["risk"] == counts["risk"]
        assert hour["total"] == counts["total"]
        assert hour["success_rate"] == counts["success_rate"]

        settings = {"stop_min_accounts": 10, "stop_min_success_rate": 65}
        stop, reason = should_stop_from_hour_window(
            jsonl, settings, now=now, cache_seconds=0
        )
        assert stop is True
        assert "近1小时" in reason
        assert "57.1%" in reason

        settings_ok = {"stop_min_accounts": 10, "stop_min_success_rate": 50}
        assert should_stop_from_hour_window(jsonl, settings_ok, now=now, cache_seconds=0)[0] is False


def test_hour_window_ignores_current_batch_only_dip():
    now = now_beijing()
    recent = (now - timedelta(minutes=5)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [{"ts": recent, "status": "ok"}] * 40 + [{"ts": recent, "status": "fail"}] * 8
    with tempfile.TemporaryDirectory() as temp:
        jsonl = Path(temp) / "register_results.jsonl"
        _write_jsonl(jsonl, rows)
        settings = {"stop_min_accounts": 10, "stop_min_success_rate": 65}
        # A unlucky current batch of 3/11 would have stopped the old metric.
        assert should_stop_low_success(3, 8, settings)[0] is True
        stop, _reason = should_stop_from_hour_window(
            jsonl, settings, now=now, cache_seconds=0
        )
        assert stop is False
        counts = window_success_counts(jsonl, hours=1, now=now, cache_seconds=0)
        assert counts["success_rate"] == 83.3


def test_v2_summary_keeps_success_stop_control():
    projected = summary(
        {
            "control": {
                "mode": "orch",
                "workers": 3,
                "stop_min_accounts": 10,
                "stop_min_success_rate": 65.0,
                "debug_mode": True,
            },
            "process": {"running": False, "orch_running": False, "batch_running": False},
            "rates": {"1h": {"ok": 8, "fail": 4, "risk": 2, "total": 14, "success_rate": 57.1}},
        }
    )
    assert projected["control"]["stop_min_accounts"] == 10
    assert projected["control"]["stop_min_success_rate"] == 65.0
    assert projected["control"]["debug_mode"] is True
    assert projected["rates"]["1h"]["success_rate"] == 57.1


if __name__ == "__main__":
    test_does_not_stop_until_attempts_exceed_floor()
    test_exactly_threshold_rate_continues()
    test_zero_rate_floor_never_stops()
    test_normalize_clamps_and_defaults()
    test_save_control_persists_success_stop()
    test_save_control_persists_debug_mode_har_flag()
    test_hour_window_matches_panel_success_stats()
    test_hour_window_ignores_current_batch_only_dip()
    test_v2_summary_keeps_success_stop_control()
    print("OK success stop")
