import json
import logging
from decimal import Decimal

import pytest
from awx_common import OperationStatus, ReadRequest, derive_request_id
from awx_common.errors import (
    DuplicateRequest,
    InsufficientFunds,
    NotFound,
    RateLimited,
    TransientServerError,
    Unauthorized,
    ValidationFailed,
)
from awx_spine.client import AirwallexClient, HostNotAllowed, _pin
from awx_spine.credentials import CredentialsUnavailable, EnvVarCredentials
from awx_spine.testing import (
    API_KEY,
    CLIENT_ID,
    TOKEN,
    TOKEN2,
    ScriptedTransport,
    creds,
    login_ok,
    make_op,
    refused,
    resp,
    timeout,
    transfer_op,
)

SECRETS = (CLIENT_ID, API_KEY, TOKEN, TOKEN2)


def client(transport, **kw):
    clock = kw.pop("clock", lambda: 1_000.0)
    return AirwallexClient(creds(), transport=transport, clock=clock, sleep=lambda s: None, **kw)


def assert_clean(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


# ---- login and token cache ----------------------------------------------------------------------


def test_login_sends_credentials_as_headers_and_reuses_token():
    t = ScriptedTransport([login_ok(), resp(200, {"items": []}), resp(200, {"items": []})])
    c = client(t)
    c.read(ReadRequest("/api/v1/transfers"))
    c.read(ReadRequest("/api/v1/transfers"))
    login = t.sent[0]
    assert login.url == "https://api.sandbox.airwallex.com/api/v1/authentication/login"
    assert login.method == "POST"
    assert login.body == b""
    assert login.headers["x-client-id"] == CLIENT_ID
    assert login.headers["x-api-key"] == API_KEY
    assert len(t.sent) == 3  # one login only
    for call in t.api_calls():
        assert call.headers["Authorization"] == f"Bearer {TOKEN}"
        assert "x-api-key" not in call.headers


def test_token_refreshes_before_expiry():
    now = [1_000.0]
    t = ScriptedTransport(
        [
            resp(201, {"token": TOKEN, "expires_at": "1970-01-01T00:30:00+0000"}),
            resp(200, {}),
            login_ok(TOKEN2),
            resp(200, {}),
        ]
    )
    c = client(t, clock=lambda: now[0])
    c.read(ReadRequest("/api/v1/balances/current"))
    now[0] = 1_800 - 60  # inside the two-minute margin
    c.read(ReadRequest("/api/v1/balances/current"))
    assert t.sent[-1].headers["Authorization"] == f"Bearer {TOKEN2}"


def test_401_refreshes_token_once_then_succeeds():
    t = ScriptedTransport(
        [login_ok(), resp(401, {"code": "credentials_expired"}), login_ok(TOKEN2), resp(200, {})]
    )
    assert client(t).read(ReadRequest("/api/v1/balances/current")) == {}
    assert t.sent[-1].headers["Authorization"] == f"Bearer {TOKEN2}"


def test_persistent_401_raises_unauthorized_without_secrets():
    t = ScriptedTransport(
        [
            login_ok(),
            resp(401, {"code": "credentials_invalid", "message": "Access Denied"}),
            login_ok(TOKEN2),
            resp(401, {"code": "credentials_invalid", "message": "Access Denied"}),
        ]
    )
    with pytest.raises(Unauthorized) as info:
        client(t).read(ReadRequest("/api/v1/balances/current"))
    assert_clean(repr(info.value) + str(info.value.details))


def test_login_refused_is_unauthorized_and_redacted():
    t = ScriptedTransport(
        [resp(401, {"code": "credentials_invalid", "message": f"bad key {API_KEY}"})]
    )
    with pytest.raises(Unauthorized) as info:
        client(t).read(ReadRequest("/api/v1/balances/current"))
    assert_clean(str(info.value))


def test_missing_credentials_become_failed_result_not_crash():
    c = AirwallexClient(EnvVarCredentials({}), transport=ScriptedTransport())
    result = c.execute(transfer_op())
    assert result.status is OperationStatus.FAILED
    assert result.error_type == "Unauthorized"
    with pytest.raises(CredentialsUnavailable):
        EnvVarCredentials({}).get()


# ---- hosts and headers --------------------------------------------------------------------------


def test_hosts_are_pinned_to_the_sandbox():
    assert _pin("https://api.sandbox.airwallex.com/api/v1/x")
    assert _pin("https://files.sandbox.airwallex.com/api/v1/files/upload")
    for url in (
        "https://api.airwallex.com/api/v1/x",
        "http://api.sandbox.airwallex.com/api/v1/x",
        "https://api.sandbox.airwallex.com.evil.example/api/v1/x",
        "https://api.sandbox.airwallex.com:8443/api/v1/x",
    ):
        with pytest.raises(HostNotAllowed):
            _pin(url)


@pytest.mark.parametrize("path", ["/api/v1/../x", "/api/v1//x", "/api/v1/x?y=1", "/api/v1/a b"])
def test_odd_paths_are_refused(path):
    with pytest.raises(ValueError, match="plain"):
        client(ScriptedTransport()).read(ReadRequest(path))


def test_on_behalf_of_header_and_no_api_version_on_fx():
    t = ScriptedTransport([login_ok(), resp(201, {"conversion_id": "c1", "status": "SETTLED"})])
    op = make_op(
        "k6",
        "fx.conversions.create",
        "/api/v1/fx/conversions/create",
        {"buy_currency": "EUR", "sell_currency": "USD", "buy_amount": Decimal("1.00")},
        "fx:1",
        on_behalf_of="acct_abc",
    )
    result = client(t).execute(op)
    assert result.status is OperationStatus.SUCCEEDED
    assert result.provider_id == "c1"
    sent = t.api_calls()[0]
    assert sent.headers["x-on-behalf-of"] == "acct_abc"
    assert not any(k.lower() == "x-api-version" for k in sent.headers)


def test_platform_money_movement_refuses_on_behalf_of_before_sending():
    t = ScriptedTransport()
    op = make_op(
        "k5",
        "connected_account_transfers.create",
        "/api/v1/connected_account_transfers/create",
        {
            "amount": Decimal("1.00"),
            "currency": "USD",
            "destination": "acct_b",
            "reason": "wages_salary",
            "reference": "poc-k5-x",
        },
        "cat:1",
        on_behalf_of="acct_a",
    )
    result = client(t).execute(op)
    assert result.status is OperationStatus.FAILED
    assert result.error_type == "TenantScopeViolation"
    assert t.sent == []


# ---- body encoding ------------------------------------------------------------------------------


def test_body_is_exact_decimal_json_and_sent_as_built():
    t = ScriptedTransport([login_ok(), resp(201, {"id": "t1", "status": "SCHEDULED"})])
    op = transfer_op("0.10")
    client(t).execute(op)
    raw = t.api_calls()[0].body.decode()
    assert '"transfer_amount":0.10' in raw
    assert json.loads(raw, parse_float=Decimal) == {**op.body}


def test_float_in_body_is_refused_before_sending():
    t = ScriptedTransport()
    op = transfer_op()
    object.__setattr__(op, "body", {**op.body, "transfer_amount": 10.0})
    with pytest.raises(ValueError, match="float"):
        client(t).execute(op)
    assert t.sent == []


def test_read_wraps_top_level_list_and_parses_decimals():
    body = b'[{"currency":"USD","available_amount":8.88,"total_amount":10000000}]'
    t = ScriptedTransport([login_ok(), resp(200, body)])
    got = client(t).read(ReadRequest("/api/v1/balances/current"))
    assert got == {
        "items": [
            {"currency": "USD", "available_amount": Decimal("8.88"), "total_amount": 10000000}
        ]
    }
    assert isinstance(got["items"][0]["available_amount"], Decimal)
    t = ScriptedTransport([login_ok(), resp(200, b'{"rate":1.5e-3}')])
    assert client(t).read(ReadRequest("/api/v1/fx/rates/current"))["rate"] == Decimal("0.0015")


def test_read_query_is_encoded():
    t = ScriptedTransport([login_ok(), resp(200, {"items": []})])
    rid = derive_request_id("k3", "x")
    client(t).read(ReadRequest("/api/v1/transfers", {"request_id": rid}))
    assert t.api_calls()[0].url.endswith(f"/api/v1/transfers?request_id={rid}")


# ---- error mapping ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "path", "code", "expected"),
    [
        (
            "transfers.create",
            "/api/v1/transfers/create",
            "balance_insufficient",
            "InsufficientFunds",
        ),
        ("charges.create", "/api/v1/charges/create", "insufficient_fund", "InsufficientFunds"),
        (
            "fx.conversions.create",
            "/api/v1/fx/conversions/create",
            "insufficient_funds",
            "InsufficientFunds",
        ),
    ],
)
def test_three_insufficient_funds_codes_normalise_to_one(name, path, code, expected):
    t = ScriptedTransport([login_ok(), resp(400, {"code": code, "message": "no money"})])
    op = make_op("k5", name, path, {"amount": Decimal("1")}, f"nsf:{code}")
    result = client(t).execute(op)
    assert result.status is OperationStatus.FAILED
    assert result.error_type == expected
    assert result.error_code == code
    assert isinstance(result.error(), InsufficientFunds)


def test_duplicate_shapes_all_normalise_to_duplicate_request():
    shapes = [
        {"code": "duplicate_request_id", "details": {"id": "t1"}, "message": "used"},
        {"code": "request_id_duplicate", "details": {"source_id": "tr_1"}, "message": "used"},
        {"code": "duplicate_request", "message": "The request ID x has been used previously."},
        {
            "code": "invalid_argument",
            "message": "request_id 'x' has been used in the past. ...",
            "source": "request_id",
        },
        {
            "code": "invalid_argument",
            "message": "The provided ID is already associated with a previous request",
        },
    ]
    for shape in shapes:
        t = ScriptedTransport([login_ok(), resp(400, shape)])
        result = client(t).execute(transfer_op())
        assert result.error_type == "DuplicateRequest", shape
    assert DuplicateRequest  # imported for the mapping


def test_5xx_on_write_is_outcome_unknown_and_not_retried():
    t = ScriptedTransport([login_ok(), resp(500, {"code": "operation_failed"})])
    result = client(t).execute(transfer_op())
    assert result.status is OperationStatus.OUTCOME_UNKNOWN
    assert len(t.api_calls()) == 1


def test_timeout_after_send_is_outcome_unknown_and_not_retried():
    t = ScriptedTransport([login_ok(), timeout()])
    result = client(t).execute(transfer_op())
    assert result.status is OperationStatus.OUTCOME_UNKNOWN
    assert result.error_type == "OutcomeUnknown"
    assert len(t.api_calls()) == 1


def test_connection_refused_is_retryable_failure_not_unknown():
    t = ScriptedTransport([login_ok(), refused()])
    result = client(t).execute(transfer_op())
    assert result.status is OperationStatus.FAILED
    assert result.error_type == "TransientServerError"
    assert result.error().retryable


def test_429_is_retried_with_identical_bytes():
    t = ScriptedTransport(
        [
            login_ok(),
            resp(429, {"code": "too_many_requests"}),
            resp(201, {"id": "t1", "status": "SCHEDULED"}),
        ]
    )
    result = client(t).execute(transfer_op())
    assert result.status is OperationStatus.SUCCEEDED
    first, second = t.api_calls()
    assert first.body == second.body


def test_429_exhausted_is_rate_limited_failure():
    t = ScriptedTransport([login_ok()] + [resp(429, {})] * 3)
    result = client(t).execute(transfer_op())
    assert result.error_type == "RateLimited"
    assert isinstance(result.error(), RateLimited)


def test_validation_400_is_not_retried():
    t = ScriptedTransport([login_ok(), resp(400, {"code": "validation_failed", "message": "bad"})])
    result = client(t).execute(transfer_op())
    assert result.status is OperationStatus.FAILED
    assert result.error_type == "ValidationFailed"
    assert len(t.api_calls()) == 1


def test_read_retries_5xx_and_connection_errors_then_raises():
    t = ScriptedTransport([login_ok(), resp(503, {}), timeout(), resp(200, {"ok": True})])
    assert client(t).read(ReadRequest("/api/v1/transfers")) == {"ok": True}
    t = ScriptedTransport([login_ok(), resp(503, {}), resp(503, {}), resp(503, {})])
    with pytest.raises(TransientServerError):
        client(t).read(ReadRequest("/api/v1/transfers"))


def test_read_404_raises_not_found_immediately():
    t = ScriptedTransport(
        [login_ok(), resp(404, {"code": "not_found", "message": "Transfer not found"})]
    )
    with pytest.raises(NotFound):
        client(t).read(ReadRequest("/api/v1/transfers/x"))
    assert len(t.api_calls()) == 1


# ---- redaction ----------------------------------------------------------------------------------


def test_no_secret_in_errors_results_or_logs(caplog):
    caplog.set_level(logging.DEBUG)
    leaky = {
        "code": "validation_failed",
        "message": f"echo Authorization: Bearer {TOKEN} x-api-key: {API_KEY}",
        "details": {"headers": {"Authorization": f"Bearer {TOKEN}"}, "client": CLIENT_ID},
    }
    t = ScriptedTransport([login_ok(), resp(400, leaky)])
    result = client(t).execute(transfer_op())
    assert_clean(repr(result))
    assert_clean(caplog.text)
    t = ScriptedTransport([login_ok(), resp(400, leaky)])
    with pytest.raises(ValidationFailed) as info:
        client(t).read(ReadRequest("/api/v1/transfers"))
    assert_clean(str(info.value) + repr(info.value.details))
    assert "***" in str(info.value)


def test_reprs_never_show_credentials():
    c = creds().get()
    assert_clean(repr(c) + str(c))
    assert_clean(repr(client(ScriptedTransport())))
    t = ScriptedTransport([login_ok(), resp(200, {})])
    client(t).read(ReadRequest("/api/v1/balances/current"))
    assert_clean(repr(t.sent))


# ---- upload -------------------------------------------------------------------------------------


def test_upload_is_multipart_on_the_files_host():
    t = ScriptedTransport(
        [
            login_ok(),
            resp(201, {"file_id": "f1", "filename": "e.jpg", "object_type": "file", "size": 3}),
        ]
    )
    got = client(t).upload(b"abc", file_name="e.jpg", content_type="image/jpeg", notes="poc-k4")
    assert got["file_id"] == "f1"
    sent = t.api_calls()[0]
    assert sent.url == "https://files.sandbox.airwallex.com/api/v1/files/upload"
    assert sent.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="e.jpg"' in sent.body
    assert b"abc" in sent.body
    assert b'name="notes"' in sent.body
    with pytest.raises(ValueError, match="bare file name"):
        client(ScriptedTransport()).upload(b"x", file_name="../x", content_type="image/jpeg")


# ---- Retry-After reaches the error; per-call read retry count ---------------------------------


def _with_headers(status, body, headers):
    from awx_spine.transport import HttpResponse

    r = resp(status, body)
    return HttpResponse(r.status, r.body, headers)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("7", 7.0), ("Tue, 06 Oct 2026 00:00:30 GMT", 30.0), ("Mon, 05 Oct 2026 23:00:00 GMT", 0.0)],
)
def test_retry_after_header_is_copied_into_details_as_seconds(value, expected):
    from datetime import UTC, datetime

    from awx_spine.normalise import error_from_response

    err = error_from_response(
        "GET",
        429,
        {"code": "too_many_requests"},
        headers={"Retry-After": value},
        now=lambda: datetime(2026, 10, 6, tzinfo=UTC),
    )
    assert err.details["retry_after_s"] == expected
    for junk in ({}, {"retry-after": "soon"}, None):
        assert "retry_after_s" not in error_from_response("GET", 429, {}, headers=junk).details


def test_retry_after_reaches_write_results_and_read_errors_through_the_client():
    t = ScriptedTransport(
        [login_ok()] + [_with_headers(429, {"code": "too_many_requests"}, {"retry-after": "3"})] * 3
    )
    result = client(t).execute(transfer_op())
    assert result.body["details"]["retry_after_s"] == 3.0
    t = ScriptedTransport(
        [login_ok()] + [_with_headers(429, {"code": "too_many_requests"}, {"Retry-After": "2"})] * 3
    )
    with pytest.raises(RateLimited) as caught:
        client(t).read(ReadRequest("/api/v1/transfers"))
    assert caught.value.details["retry_after_s"] == 2.0


def test_read_retry_count_can_be_lowered_per_call():
    t = ScriptedTransport([login_ok(), resp(503, {}), resp(200, {"ok": True})])
    with pytest.raises(TransientServerError):
        client(t).read(ReadRequest("/api/v1/transfers"), retries=0)
    assert len(t.api_calls()) == 1
    t = ScriptedTransport([login_ok(), resp(503, {}), resp(503, {}), resp(200, {"ok": True})])
    assert client(t).read(ReadRequest("/api/v1/transfers")) == {"ok": True}  # default unchanged
    with pytest.raises(ValueError, match="retries"):
        client(t).read(ReadRequest("/api/v1/transfers"), retries=-1)
