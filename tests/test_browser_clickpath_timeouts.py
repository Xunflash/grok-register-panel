# -*- coding: utf-8 -*-
"""Click-path budgets must match the shipped 上游 browser flow (not 打码)."""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from register_flow import (  # noqa: E402
    PROFILE_TIMEOUT,
    fill_email_and_submit,
    fill_profile_and_submit,
)


def test_fill_profile_uses_shipped_profile_timeout_when_none():
    assert PROFILE_TIMEOUT >= 120
    source = inspect.getsource(fill_profile_and_submit)
    assert "if timeout is None:" in source
    assert "timeout = PROFILE_TIMEOUT" in source
    default = inspect.signature(fill_profile_and_submit).parameters["timeout"].default
    assert default is None


def test_fill_email_default_timeout_is_upstream_45s():
    default = inspect.signature(fill_email_and_submit).parameters["timeout"].default
    assert default == 45
    source = inspect.getsource(fill_email_and_submit)
    assert "deadline = time.time() + timeout" in source
