# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from auto_register import (  # noqa: E402
    DEFAULT_COOLDOWN_MIN,
    DEFAULT_THRESHOLD,
    normalize_auto_register,
    run_auto_register_tick,
    should_auto_register,
)


NOW = 1_800_000_000.0


def _decide(**overrides):
    params = {
        "available": 50,
        "threshold": 100,
        "cooldown_minutes": 120,
        "last_auto_at": None,
        "now": NOW,
        "enabled": True,
        "already_running": False,
    }
    params.update(overrides)
    return should_auto_register(**params)


def test_available_below_threshold_starts():
    assert _decide(available=99, threshold=100) is True
    assert _decide(available=0, threshold=100) is True


def test_available_at_or_above_threshold_does_not_start():
    assert _decide(available=100, threshold=100) is False
    assert _decide(available=250, threshold=100) is False


def test_cooldown_blocks_until_y_minutes_elapse():
    last = NOW - (120 * 60) + 1
    assert _decide(available=10, last_auto_at=last, cooldown_minutes=120) is False
    later = NOW + (120 * 60)
    assert (
        should_auto_register(
            available=10,
            threshold=100,
            cooldown_minutes=120,
            last_auto_at=last,
            now=later,
            enabled=True,
            already_running=False,
        )
        is True
    )


def test_already_running_does_not_start_second_job():
    assert _decide(available=1, already_running=True) is False


def test_disabled_does_not_start():
    assert _decide(available=1, enabled=False) is False


def test_missing_available_does_not_start():
    assert _decide(available=None) is False


def test_normalize_defaults_and_clamps():
    assert normalize_auto_register({}) == {
        "auto_register_threshold": DEFAULT_THRESHOLD,
        "auto_register_cooldown_min": DEFAULT_COOLDOWN_MIN,
        "auto_register_enabled": True,
        "auto_register_last_at": None,
    }
    out = normalize_auto_register(
        {
            "auto_register_threshold": "0",
            "auto_register_cooldown_min": "99999",
            "auto_register_enabled": "off",
            "auto_register_last_at": "12.5",
        }
    )
    assert out["auto_register_threshold"] == 1
    assert out["auto_register_cooldown_min"] == 10080
    assert out["auto_register_enabled"] is False
    assert out["auto_register_last_at"] == 12.5


def test_tick_calls_start_once_and_skips_when_running():
    calls = []

    def start():
        calls.append("start")
        return {"ok": True, "pid": 7}

    started = run_auto_register_tick(
        control={"auto_register_threshold": 100, "auto_register_enabled": True},
        now=NOW,
        already_running=False,
        available=40,
        start=start,
    )
    assert started["started"] is True
    assert calls == ["start"]

    skipped = run_auto_register_tick(
        control={"auto_register_threshold": 100, "auto_register_enabled": True},
        now=NOW,
        already_running=True,
        available=40,
        start=start,
    )
    assert skipped["started"] is False
    assert calls == ["start"]


def test_tick_skips_when_start_refuses():
    def start():
        return {"ok": False, "error": "already running"}

    out = run_auto_register_tick(
        control={"auto_register_enabled": True, "auto_register_threshold": 100},
        now=NOW,
        already_running=False,
        available=1,
        start=start,
    )
    assert out["started"] is False


def test_panel_page_has_auto_register_fields():
    page = (ROOT / "webui" / "monitor.py").read_text(encoding="utf-8")
    assert 'id="auto_register_threshold"' in page
    assert 'value="100"' in page
    assert 'id="auto_register_cooldown_min"' in page
    assert 'value="120"' in page
    assert "自动注册阈值" in page
    assert "自动注册冷却分钟" in page


def test_monitor_tick_records_last_auto_activation():
    from webui import monitor

    with tempfile.TemporaryDirectory() as temp:
        control_file = Path(temp) / "monitor_control.json"
        previous = monitor.CONTROL_FILE
        previous_available = monitor._read_grok2api_available
        previous_start = monitor.start_orch
        previous_running = monitor.process_running
        previous_refresh = monitor._refresh_status_snapshot
        monitor.CONTROL_FILE = control_file
        monitor.save_control(
            {
                "auto_register_threshold": 100,
                "auto_register_cooldown_min": 120,
                "auto_register_enabled": True,
            }
        )
        starts = []

        def fake_start():
            starts.append("orch")
            return {"ok": True, "pid": 99}

        monitor._read_grok2api_available = lambda: 12
        monitor.start_orch = fake_start
        monitor.process_running = lambda: {
            "running": False,
            "orch_running": False,
            "batch_running": False,
        }
        monitor._refresh_status_snapshot = lambda: None
        try:
            first = monitor.tick_auto_register_live(now=NOW)
            assert first["started"] is True
            assert starts == ["orch"]
            disk = json.loads(control_file.read_text(encoding="utf-8"))
            assert disk["auto_register_last_at"] == NOW

            second = monitor.tick_auto_register_live(now=NOW + 60)
            assert second["started"] is False
            assert starts == ["orch"]

            later = monitor.tick_auto_register_live(now=NOW + 120 * 60)
            assert later["started"] is True
            assert starts == ["orch", "orch"]
        finally:
            monitor.CONTROL_FILE = previous
            monitor._read_grok2api_available = previous_available
            monitor.start_orch = previous_start
            monitor.process_running = previous_running
            monitor._refresh_status_snapshot = previous_refresh


if __name__ == "__main__":
    test_available_below_threshold_starts()
    test_available_at_or_above_threshold_does_not_start()
    test_cooldown_blocks_until_y_minutes_elapse()
    test_already_running_does_not_start_second_job()
    test_disabled_does_not_start()
    test_missing_available_does_not_start()
    test_normalize_defaults_and_clamps()
    test_tick_calls_start_once_and_skips_when_running()
    test_tick_skips_when_start_refuses()
    test_panel_page_has_auto_register_fields()
    test_monitor_tick_records_last_auto_activation()
    print("OK auto register")
