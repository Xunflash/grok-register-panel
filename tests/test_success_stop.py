# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from success_stop import (  # noqa: E402
    normalize_success_stop,
    should_stop_low_success,
    success_rate_percent,
)


def test_does_not_stop_until_attempts_exceed_floor():
    settings = {"stop_min_accounts": 10, "stop_min_success_rate": 65}
    stop, _reason = should_stop_low_success(0, 10, settings)
    assert stop is False
    stop, reason = should_stop_low_success(3, 8, settings)
    assert stop is True
    assert "11" in reason
    assert "27.3%" in reason


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


if __name__ == "__main__":
    test_does_not_stop_until_attempts_exceed_floor()
    test_exactly_threshold_rate_continues()
    test_zero_rate_floor_never_stops()
    test_normalize_clamps_and_defaults()
    test_save_control_persists_success_stop()
    print("OK success stop")
