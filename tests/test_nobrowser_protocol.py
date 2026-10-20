# -*- coding: utf-8 -*-
"""HAR auth trio + no-Camoufox adapters. Drives shipped builders, not copies."""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "cffi_har_signup"))

from cffi_headers import assert_no_grpc_signup_paths, har_auth_name_contract, request_json_keys
from signup_protocol import (
    AUTH_CREATE_ACCOUNT,
    AUTH_SEND_CODE,
    AUTH_VERIFY_EMAIL,
    SIGNUP_PATHS,
    apply_live_otp,
    assert_send_code_has_castle,
    build_create_account,
    build_send_verification_code,
    build_verify_email,
    json_keys_for_path,
    load_har,
)

FIXTURE = ROOT / "cffi_har_signup" / "tests" / "fixtures" / "signup_har_excerpt.json"
if not FIXTURE.is_file():
    FIXTURE = ROOT / "tests" / "fixtures" / "signup_har_excerpt.json"


def test_shipped_builders_match_har_auth_name_contract():
    har = load_har(FIXTURE)
    names = har_auth_name_contract(har)
    send = build_send_verification_code(email="a@b.c", castle_request_token="live-castle")
    verify = build_verify_email(email="a@b.c", code="123-456")
    create = build_create_account(
        email="a@b.c",
        password="pw",
        given_name="A",
        family_name="B",
        email_validation_code="123-456",
        turnstile_token="ts",
        castle_request_token="live-castle",
    )
    assert_send_code_has_castle(send)
    assert_no_grpc_signup_paths([send["url"], verify["url"], create["url"]])
    assert set(request_json_keys(send)) == {"castleRequestToken", "email"}
    assert set(request_json_keys(verify)) == {"code", "email"}
    assert set(request_json_keys(create)) == {
        "email",
        "emailValidationCode",
        "familyName",
        "givenName",
        "password",
        "turnstileToken",
        "castleRequestToken",
    }
    assert set(names["calls"][AUTH_SEND_CODE]["json_keys"]) == {"email", "castleRequestToken"}
    assert set(names["calls"][AUTH_VERIFY_EMAIL]["json_keys"]) == {"code", "email"}
    har_create = set(json_keys_for_path(har, AUTH_CREATE_ACCOUNT) or [])
    assert {
        "email",
        "password",
        "givenName",
        "familyName",
        "emailValidationCode",
        "turnstileToken",
    } <= har_create
    extra = set(request_json_keys(create)) - har_create
    assert extra <= {"castleRequestToken"}
    assert send["path"] == AUTH_SEND_CODE
    assert verify["path"] == AUTH_VERIFY_EMAIL
    assert create["path"] == AUTH_CREATE_ACCOUNT
    live_verify = apply_live_otp(verify, "123-456", "code")
    assert live_verify["json"]["code"] == "123-456"
    assert set(request_json_keys(live_verify)) == {"code", "email"}
    live_create = apply_live_otp(create, "123-456", "emailValidationCode")
    assert live_create["json"]["emailValidationCode"] == "123-456"
    blob = json.dumps({"send": send, "verify": verify, "create": create})
    assert "CreateEmailValidationCode" not in blob
    assert "CreateUserAndSessionV2" not in blob


def _function_imported_modules(path: Path, func_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            for child in ast.walk(node):
                if isinstance(child, ast.Import):
                    for alias in child.names:
                        names.add(alias.name.split(".")[0])
                elif isinstance(child, ast.ImportFrom) and child.module:
                    names.add(child.module.split(".")[0])
    return names


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def test_page_engine_is_patchright_not_playwright_or_camoufox():
    engine = ROOT / "nobrowser_protocol" / "patchright_engine.py"
    run = ROOT / "nobrowser_protocol" / "run_live.py"
    cffi = ROOT / "nobrowser_protocol" / "cffi_engine.py"
    for path in (cffi, engine, run):
        text = path.read_text(encoding="utf-8")
        assert "camoufox_adapter" not in text
        assert "from camoufox" not in text
        assert "import camoufox" not in text
        assert "start_browser" not in text
        imported = _imported_names(path)
        assert "camoufox_adapter" not in imported
        assert "camoufox" not in imported
        assert "browser_session" not in imported
    engine_text = engine.read_text(encoding="utf-8")
    run_text = run.read_text(encoding="utf-8")
    sdk_text = (ROOT / "nobrowser_protocol" / "page_sdk.py").read_text(encoding="utf-8")
    assert "from patchright.sync_api import sync_playwright" in engine_text
    assert "from playwright.sync_api" not in engine_text
    assert "Sign up with email" in engine_text
    assert "page_send_code_request" in engine_text
    assert "page_request_headers" in engine_text
    assert "_castle_from_capture_or_mint" in engine_text
    assert "_send_code_succeeded" in engine_text
    assert "page_otp_form" in engine_text
    assert "Confirm email" in engine_text
    assert "with apple" in engine_text
    assert "CLICK_VISIBLE_JS" in sdk_text
    assert "SUBMIT_EMAIL_JS" in sdk_text
    assert "FILL_OTP_JS" in sdk_text
    assert "import playwright" not in engine_text
    assert "python -m playwright install" not in engine_text
    assert "python -m playwright install" not in run_text
    assert "from playwright.sync_api" not in run_text
    assert "run_patchright_one" in run_text
    assert "def signup_with_patchright(" in engine_text
    assert "apply_live_otp" in engine_text
    assert "from outlook_min import apply_live_otp" not in engine_text
    pr_imports = _function_imported_modules(engine, "signup_with_patchright")
    assert "outlook_min" not in pr_imports
    assert "camoufox" not in pr_imports
    assert "patchright" in pr_imports
    assert "run_patchright_sync" in engine_text
    assert "get_running_loop" in engine_text
    assert 'cfg.get("guard_url")' in run_text
    scheme = (ROOT / "signup_scheme.py").read_text(encoding="utf-8")
    assert "SCHEME_PROTOCOL_PATCHRIGHT" in scheme
    assert "signup_with_patchright" in scheme
    from webui.api_projection import CONTROL_ENUMS

    assert "protocol_patchright" in CONTROL_ENUMS["registration_scheme"]
    from nobrowser_protocol.cffi_engine import camoufox_imports
    from nobrowser_protocol.patchright_engine import (
        _castle_from_capture_or_mint,
        _send_code_resend_allowed,
        _send_code_succeeded,
        camoufox_imports as pr_camoufox,
    )

    assert camoufox_imports() == ()
    assert pr_camoufox() == ()
    live = _castle_from_capture_or_mint(
        {"token": "from-xhr", "how": "page_send_code_request"},
        {"token": "from-mint", "how": "Castle.createRequestToken"},
    )
    assert live["token"] == "from-xhr"
    assert live["how"] == "page_send_code_request"
    minted = _castle_from_capture_or_mint(
        {"token": "", "how": "page_send_code_timeout"},
        {"token": "from-mint", "how": "Castle.createRequestToken", "pk": "pk_x"},
    )
    assert minted["token"] == "from-mint"
    assert minted["how"] == "Castle.createRequestToken"
    empty = _castle_from_capture_or_mint({}, {})
    assert empty["token"] == ""
    from signup_scheme import (
        ENV_NAME,
        SCHEME_PROTOCOL,
        SCHEME_PROTOCOL_PATCHRIGHT,
        maybe_run_protocol_signup,
        resolve_registration_scheme,
    )

    assert resolve_registration_scheme("协议注册（patchright）") == SCHEME_PROTOCOL_PATCHRIGHT
    assert resolve_registration_scheme("patchwright") == SCHEME_PROTOCOL_PATCHRIGHT
    assert resolve_registration_scheme("协议注册") == SCHEME_PROTOCOL
    os_environ = __import__("os").environ
    old = os_environ.get(ENV_NAME)
    os_environ[ENV_NAME] = SCHEME_PROTOCOL_PATCHRIGHT
    try:
        events = []

        def boom(*_a, **_k):
            events.append("camoufox")
            raise AssertionError("camoufox")

        def fake_run(log_callback=None, cancel_callback=None):
            events.append("patchright")
            return {"email": "a@b.c", "profile": {"password": "pw"}, "sso": "sso"}

        import signup_scheme as scheme_mod
        from unittest.mock import patch

        with patch.object(scheme_mod, "_prepare_protocol_browser", boom), patch.object(
            scheme_mod, "_close_protocol_browser", boom
        ), patch.object(scheme_mod, "_load_protocol_signup", boom), patch.object(
            scheme_mod, "_load_patchright_signup", lambda: fake_run
        ):
            out = maybe_run_protocol_signup(lambda _m: None, lambda: False)
        assert events == ["patchright"]
        assert out["scheme"] == SCHEME_PROTOCOL_PATCHRIGHT
    finally:
        if old is None:
            os_environ.pop(ENV_NAME, None)
        else:
            os_environ[ENV_NAME] = old
    assert _send_code_succeeded({"token": "x"}) is False
    assert _send_code_succeeded({"token": "x", "send_status": 200}) is True
    assert _send_code_succeeded({"token": "x", "send_status": 502}) is False
    assert _send_code_succeeded({"how": "page_otp_form"}) is True
    assert _send_code_succeeded({}, otp_visible=True) is True
    assert _send_code_succeeded({"how": "page_send_code_timeout"}, otp_visible=False) is False
    assert _send_code_resend_allowed("") is True
    assert _send_code_resend_allowed("account:email-signup-unavailable") is False
    assert _send_code_resend_allowed("email:send-validation-code-rate-limited") is False
    assert _send_code_resend_allowed("form-invalid:email:email") is False
    assert _send_code_resend_allowed("account:missing-email") is True
    from nobrowser_protocol.patchright_engine import _otp_for_page

    assert _otp_for_page("123-456") == "123456"
    assert _otp_for_page("ABC123456") == "123456"
    assert len(_otp_for_page("12-34-56")) == 6
    assert 'input[type="email"], input[name="email"]' not in sdk_text
    assert 'input[data-testid="email"]' in sdk_text
    assert "__reactProps$" in sdk_text
    assert "autocomplete') || '').toLowerCase()" in sdk_text
    assert "no-visible-email" in sdk_text
    assert "EMAIL_INPUT_SELECTOR" in engine_text
    assert "https://grok.com/sign-up" in engine_text
    assert "cookie-chain" in engine_text
    assert "INJECT_TURNSTILE_JS" in sdk_text
    assert "givenName" in sdk_text
    assert "press_sequentially" in engine_text
    assert '_resend_verification_code(page, captured, emit, email)' in engine_text
    assert "send-code resend" in engine_text
    assert "skipped submit; email field empty" in engine_text


def test_signup_with_patchright_reaches_browser_without_outlook_min():
    """Call the shipped panel entry far enough to prove apply_live_otp imported."""
    import sys
    import types

    sys.modules.pop("outlook_min", None)
    fake_patchright = types.ModuleType("patchright")
    fake_api = types.ModuleType("patchright.sync_api")

    class _PW:
        def __enter__(self):
            raise RuntimeError("sync_playwright-entered")

        def __exit__(self, *args):
            return False

    fake_api.sync_playwright = lambda: _PW()
    sys.modules["patchright"] = fake_patchright
    sys.modules["patchright.sync_api"] = fake_api
    from nobrowser_protocol.patchright_engine import signup_with_patchright

    raised = None
    try:
        signup_with_patchright(
            har={},
            proxy="",
            email="a@b.c",
            wait_otp=lambda _t: "427-599",
            solve_ts=lambda _k: "ts",
            log=lambda _m: None,
        )
    except Exception as exc:
        raised = exc
    finally:
        sys.modules.pop("patchright", None)
        sys.modules.pop("patchright.sync_api", None)
    assert raised is not None
    assert "outlook_min" not in str(raised)
    assert "sync_playwright-entered" in str(raised)
    assert "outlook_min" not in sys.modules


def test_signup_with_patchright_survives_running_asyncio_loop():
    """Shipped panel entry must get past Patchright driver enter inside asyncio.run()."""
    import asyncio
    import inspect
    import sys

    sys.modules.pop("patchright", None)
    sys.modules.pop("patchright.sync_api", None)
    from nobrowser_protocol.patchright_engine import run_patchright_sync, signup_with_patchright

    src = inspect.getsource(signup_with_patchright)
    assert "run_patchright_sync" in src
    assert "from patchright.sync_api import sync_playwright" in src
    assert "from playwright.sync_api" not in src

    hop = {}

    async def prove_hop():
        def body():
            try:
                asyncio.get_running_loop()
                hop["child_loop"] = True
            except RuntimeError:
                hop["child_loop"] = False
            hop["thread"] = __import__("threading").current_thread().name
            return "hop-ok"

        return run_patchright_sync(body)

    assert asyncio.run(prove_hop()) == "hop-ok"
    assert hop.get("child_loop") is False
    assert hop.get("thread") == "patchright-sync"

    logs: list[str] = []
    errors: list[str] = []

    async def call_shipped_entry():
        try:
            signup_with_patchright(
                har={},
                proxy="",
                email="asyncio-loop@example.com",
                wait_otp=lambda _t: "000-000",
                solve_ts=lambda _k: "ts",
                log=logs.append,
            )
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    asyncio.run(call_shipped_entry())
    blob = "\n".join(logs + errors)
    assert "Playwright Sync API inside the asyncio loop" not in blob
    assert "inside the asyncio loop" not in blob
    assert any("engine=patchright" in msg for msg in logs)
    try:
        import patchright  # noqa: F401

        have_patchright = True
    except ImportError:
        have_patchright = False
    if have_patchright:
        assert any("patchright driver entered" in msg for msg in logs), blob[:2000]
    else:
        assert errors and "patchright" in errors[0].lower()


def test_existing_email_payload_and_engine_raises():
    from nobrowser_protocol.patchright_engine import (
        ExistingEmailError,
        existing_email_payload,
    )

    assert existing_email_payload(
        {"existingEmail": "old@outlook.com", "signInMethods": ["password"]}
    )
    assert existing_email_payload({"signInMethods": ["google"]})
    assert not existing_email_payload({"session": "sso-token-value-long-enough"})
    assert not existing_email_payload("nope")
    engine = (ROOT / "nobrowser_protocol" / "patchright_engine.py").read_text(
        encoding="utf-8"
    )
    scheme = (ROOT / "signup_scheme.py").read_text(encoding="utf-8")
    ttk = (ROOT / "grok_register_ttk.py").read_text(encoding="utf-8")
    assert "raise ExistingEmailError" in engine
    assert "existing_email" in scheme
    assert "retry_patchright_mailboxes" in scheme
    assert "mark_outlook_rt_used" in scheme
    assert "def mark_outlook_rt_used(" in ttk
    err = ExistingEmailError("a@b.c", {"existingEmail": True, "signInMethods": ["p"]})
    assert err.email == "a@b.c"
    assert "existingEmail=a@b.c" in str(err)


def test_patchright_panel_retries_existing_email_and_marks_used():
    from nobrowser_protocol.patchright_engine import ExistingEmailError
    from signup_scheme import retry_patchright_mailboxes

    calls = {"mail": 0, "marked": []}

    def take_mailbox():
        calls["mail"] += 1
        return f"box{calls['mail']}@outlook.com", f"tok{calls['mail']}"

    def signup(email, _token):
        if email.startswith("box1") or email.startswith("box2"):
            raise ExistingEmailError(
                email, {"existingEmail": email, "signInMethods": ["password"]}
            )
        return {
            "email": email,
            "sso": "sso-from-third",
            "password": "pw",
            "given_name": "A",
            "family_name": "B",
        }

    def mark_used(email):
        calls["marked"].append(email)

    logs = []
    out = retry_patchright_mailboxes(
        take_mailbox=take_mailbox,
        signup=signup,
        mark_used=mark_used,
        log=logs.append,
        cancel=lambda: None,
        max_tries=3,
    )
    assert out["email"] == "box3@outlook.com"
    assert out["sso"] == "sso-from-third"
    assert calls["mail"] == 3
    assert calls["marked"] == ["box1@outlook.com", "box2@outlook.com"]
    assert any("existingEmail=box1@outlook.com" in line for line in logs)
    assert "retry_patchright_mailboxes" in (
        ROOT / "signup_scheme.py"
    ).read_text(encoding="utf-8")


def test_send_code_rejection_switches_exit_and_mailbox():
    from nobrowser_protocol.patchright_engine import (
        SendCodeSwitchError,
        raise_send_code_rejection,
    )
    from signup_scheme import retry_patchright_mailboxes

    for code in (
        "account:email-signup-unavailable",
        "email:send-validation-code-rate-limited",
    ):
        try:
            raise_send_code_rejection(400, code, "a@outlook.com")
        except SendCodeSwitchError as exc:
            assert exc.code == code
            assert exc.email == "a@outlook.com"
        else:
            raise AssertionError(code)
    try:
        raise_send_code_rejection(400, "account:email-domain-rejected", "a@outlook.com")
    except RuntimeError as exc:
        assert "account:email-domain-rejected" in str(exc)
    else:
        raise AssertionError("domain rejection stays a hard error")

    calls = {"mail": 0, "marked": [], "rotated": 0}

    def take_mailbox():
        calls["mail"] += 1
        return f"box{calls['mail']}@outlook.com", f"tok{calls['mail']}"

    def signup(email, _token):
        if email == "box1@outlook.com":
            raise SendCodeSwitchError("account:email-signup-unavailable", email)
        if email == "box2@outlook.com":
            raise SendCodeSwitchError("email:send-validation-code-rate-limited", email)
        return {
            "email": email,
            "sso": "sso",
            "password": "pw",
            "given_name": "A",
            "family_name": "B",
        }

    def mark_used(email, reason="existing_email"):
        calls["marked"].append((email, reason))

    def rotate_exit():
        calls["rotated"] += 1

    out = retry_patchright_mailboxes(
        take_mailbox=take_mailbox,
        signup=signup,
        mark_used=mark_used,
        log=lambda _m: None,
        cancel=lambda: None,
        max_tries=3,
        rotate_exit=rotate_exit,
    )
    assert out["email"] == "box3@outlook.com"
    assert calls["rotated"] == 2
    assert calls["marked"] == [
        ("box1@outlook.com", "send_code_switch"),
        ("box2@outlook.com", "send_code_switch"),
    ]


def test_outlook_code_miss_switches_mailbox_and_does_not_count():
    from retry_policy import should_count_as_failure
    from signup_scheme import retry_patchright_mailboxes

    calls = {"mail": 0, "marked": []}

    def take_mailbox():
        calls["mail"] += 1
        return f"box{calls['mail']}@outlook.com", f"tok{calls['mail']}"

    def signup(email, _token):
        if email != "box2@outlook.com":
            raise RuntimeError("Outlook RT 在 180s 内未收到验证码邮件")
        return {"email": email, "sso": "sso", "password": "pw", "given_name": "A", "family_name": "B"}

    def mark_used(email):
        calls["marked"].append(email)

    out = retry_patchright_mailboxes(
        take_mailbox=take_mailbox,
        signup=signup,
        mark_used=mark_used,
        log=lambda _m: None,
        cancel=lambda: None,
        max_tries=3,
    )
    assert out["email"] == "box2@outlook.com"
    assert calls["marked"] == ["box1@outlook.com"]
    miss = RuntimeError("Outlook RT 在 180s 内未收到验证码邮件")
    assert should_count_as_failure(miss) is False
    switched = RuntimeError(
        "send-code account:email-signup-unavailable，已换 3 个出口和邮箱 last=a@outlook.com"
    )
    limited = RuntimeError(
        "send-code email:send-validation-code-rate-limited，已换 3 个出口和邮箱 last=a@outlook.com"
    )
    changed = RuntimeError(
        "Page.goto: net::ERR_NETWORK_CHANGED at https://accounts.x.ai/sign-up?redirect=grok-com"
    )
    assert should_count_as_failure(switched) is False
    assert should_count_as_failure(limited) is False
    assert should_count_as_failure(changed) is False
    assert should_count_as_failure(RuntimeError("send-code not accepted status=400")) is True


def test_patchright_pre_inbox_retries_unspent_then_counts_spent():
    from retry_policy import ReusableResourceError, should_count_as_failure
    from signup_scheme import run_patchright_pre_inbox

    calls = {"n": 0, "recycle": 0}
    logs = []

    def run_once(_usage):
        calls["n"] += 1
        raise RuntimeError("Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED")

    def recycle():
        calls["recycle"] += 1

    try:
        run_patchright_pre_inbox(
            run_once,
            recycle=recycle,
            log_fn=logs.append,
            attempts=3,
        )
    except ReusableResourceError as exc:
        assert "ERR_TUNNEL_CONNECTION_FAILED" in str(exc)
        assert should_count_as_failure(exc) is False
    else:
        raise AssertionError("unspent tunnel error must be ReusableResourceError")
    assert calls["n"] == 3
    assert calls["recycle"] == 2
    assert any("收件前失败" in line for line in logs)

    spent_calls = {"n": 0, "recycle": 0}

    def run_spent(usage):
        spent_calls["n"] += 1
        usage.email_used = True
        raise RuntimeError("no sso keys=['existingEmail', 'signInMethods']")

    try:
        run_patchright_pre_inbox(
            run_spent,
            recycle=lambda: spent_calls.__setitem__("recycle", spent_calls["recycle"] + 1),
            log_fn=lambda _m: None,
            attempts=3,
        )
    except RuntimeError as exc:
        assert "existingEmail" in str(exc)
        assert should_count_as_failure(exc) is True
    else:
        raise AssertionError("spent mailbox error must count as failure")
    assert spent_calls["n"] == 1
    assert spent_calls["recycle"] == 0

    cap_calls = {"n": 0}

    def run_cap(usage):
        cap_calls["n"] += 1
        usage.remote_captcha_used = True
        raise RuntimeError("400 Client Error: Bad Request for url: https://api.capsolver.com/getTaskResult")

    try:
        run_patchright_pre_inbox(
            run_cap,
            recycle=lambda: None,
            log_fn=lambda _m: None,
            attempts=4,
        )
    except RuntimeError as exc:
        assert "capsolver" in str(exc).lower()
        assert should_count_as_failure(exc) is True
    else:
        raise AssertionError("spent captcha error must count as failure")
    assert cap_calls["n"] == 1

    engine = (ROOT / "nobrowser_protocol" / "patchright_engine.py").read_text(
        encoding="utf-8"
    )
    scheme = (ROOT / "signup_scheme.py").read_text(encoding="utf-8")
    assert "usage.email_used = True" in engine
    assert "usage.remote_captcha_used = True" in engine
    assert "run_patchright_pre_inbox" in scheme
    assert "recycle_patchright_exit" in scheme
    assert "restart_browser" not in scheme.split("def recycle_patchright_exit")[1].split("def ")[0]


def test_panel_worker_loads_patchright_otp_without_outlook_min():
    """Panel release tree has no cffi_har_signup; worker must not need outlook_min."""
    import importlib
    import sys

    sys.modules.pop("outlook_min", None)
    old_path = sys.path[:]
    sys.path[:] = [p for p in sys.path if "cffi_har_signup" not in str(p)]
    try:
        import signup_protocol

        importlib.reload(signup_protocol)
        live = signup_protocol.apply_live_otp(
            signup_protocol.build_verify_email(email="a@b.c", code="427-599"),
            "427-599",
            "code",
        )
        assert live["json"]["code"] == "427-599"
        import nobrowser_protocol.patchright_engine as pe

        importlib.reload(pe)
        assert pe.apply_live_otp is signup_protocol.apply_live_otp
        assert callable(pe.signup_with_patchright)
        assert "outlook_min" not in sys.modules
    finally:
        sys.path[:] = old_path


if __name__ == "__main__":
    test_shipped_builders_match_har_auth_name_contract()
    test_page_engine_is_patchright_not_playwright_or_camoufox()
    test_signup_with_patchright_reaches_browser_without_outlook_min()
    test_signup_with_patchright_survives_running_asyncio_loop()
    test_existing_email_payload_and_engine_raises()
    test_patchright_panel_retries_existing_email_and_marks_used()
    test_patchright_pre_inbox_retries_unspent_then_counts_spent()
    test_panel_worker_loads_patchright_otp_without_outlook_min()
    print("OK nobrowser protocol")
