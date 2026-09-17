#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from retry_policy import (
    ProtocolResourceUsage,
    ReusableResourceError,
    retry_protocol_pre_inbox,
)


def test_pre_inbox_retries_then_raises_reusable():
    calls = {"n": 0, "recycle": 0}
    logs = []

    def run_once(usage):
        calls["n"] += 1
        raise RuntimeError("live companion POSTs missing HAR classes ['observability']")

    def recycle():
        calls["recycle"] += 1

    try:
        retry_protocol_pre_inbox(
            run_once,
            recycle=recycle,
            attempts=3,
            log_fn=logs.append,
        )
    except ReusableResourceError as exc:
        assert "observability" in str(exc)
    else:
        raise AssertionError("expected ReusableResourceError")
    assert calls["n"] == 3
    assert calls["recycle"] == 2
    assert any("收件前失败" in line for line in logs)


def test_pre_inbox_stops_retrying_after_email_used():
    calls = {"n": 0, "recycle": 0}

    def run_once(usage: ProtocolResourceUsage):
        calls["n"] += 1
        usage.email_used = True
        raise RuntimeError("send-code 400")

    def recycle():
        calls["recycle"] += 1

    try:
        retry_protocol_pre_inbox(
            run_once,
            recycle=recycle,
            attempts=3,
            log_fn=lambda _m: None,
        )
    except RuntimeError as exc:
        assert "send-code 400" in str(exc)
    else:
        raise AssertionError("email-used error must propagate")
    assert calls["n"] == 1
    assert calls["recycle"] == 0


def test_pre_inbox_stops_retrying_after_remote_captcha():
    calls = {"n": 0}

    def run_once(usage: ProtocolResourceUsage):
        calls["n"] += 1
        usage.remote_captcha_used = True
        raise RuntimeError("capsolver timeout")

    try:
        retry_protocol_pre_inbox(
            run_once,
            recycle=lambda: None,
            attempts=4,
            log_fn=lambda _m: None,
        )
    except RuntimeError as exc:
        assert "capsolver" in str(exc)
    else:
        raise AssertionError("captcha-used error must propagate")
    assert calls["n"] == 1


def test_pre_inbox_succeeds_after_transient_companion_gap():
    calls = {"n": 0}

    def run_once(_usage):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("live companion POSTs missing HAR classes ['observability']")
        return {"sso": "ok", "email": "a@b.c"}

    out = retry_protocol_pre_inbox(
        run_once,
        recycle=lambda: None,
        attempts=3,
        log_fn=lambda _m: None,
    )
    assert out["sso"] == "ok"
    assert calls["n"] == 3


if __name__ == "__main__":
    test_pre_inbox_retries_then_raises_reusable()
    test_pre_inbox_stops_retrying_after_email_used()
    test_pre_inbox_stops_retrying_after_remote_captcha()
    test_pre_inbox_succeeds_after_transient_companion_gap()
    print("OK protocol pre-inbox retry")
