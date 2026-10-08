"""AirwallexClient: the only code that sends a request to Airwallex.

Hosts are pinned to the sandbox. Login exchanges the client id and API key for a 30-minute bearer
token, cached until two minutes before it expires; a 401 refreshes it once. Request bodies are
exact Decimal JSON. Every failure comes back normalised (awx_common.errors) and redacted.

Retry rules, deliberately narrow:
- execute() resends only on 429 (the provider refused the request, nothing happened), and always
  resends the identical bytes, so the request_id never changes. A 5xx or a timeout on a write is
  returned as OUTCOME_UNKNOWN and never retried here: the MutationExecutor reads the object back
  first.
- read() also retries 5xx and connection failures, because a GET changes nothing.

The client never sends x-api-version (or any header beyond auth, content type, user agent and
x-on-behalf-of). Sandbox, 6 Oct: a stale x-api-version on fx/conversions/create and
fx/rates/current returns 400 incorrect_version; omitting it works.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

from awx_common import (
    PLATFORM_BODY_FIELDS,
    Operation,
    OperationResult,
    OperationStatus,
    ReadRequest,
)
from awx_common.errors import (
    AwxError,
    OutcomeUnknown,
    TenantScopeViolation,
    TransientServerError,
    Unauthorized,
)
from awx_common.tenancy import is_account_id

from .credentials import CredentialsProvider, CredentialsUnavailable
from .normalise import error_from_response, result_from_error, result_from_response
from .redact import Redactor
from .transport import HttpRequest, HttpResponse, Transport, TransportError, UrllibTransport
from .wire import decode_json, encode_json

SANDBOX_API_BASE = "https://api.sandbox.airwallex.com"
SANDBOX_FILES_BASE = "https://files.sandbox.airwallex.com"
ALLOWED_HOSTS = frozenset({"api.sandbox.airwallex.com", "files.sandbox.airwallex.com"})
LOGIN_PATH = "/api/v1/authentication/login"
TOKEN_REFRESH_MARGIN_S = 120.0
DEFAULT_TOKEN_LIFETIME_S = 25 * 60.0
USER_AGENT = "awx-spine/0.1 (aws-poc; sandbox)"

log = logging.getLogger("awx_spine.client")


class HostNotAllowed(Exception):
    """A URL outside the pinned sandbox hosts. Never raised for a well-formed /api/v1 path."""


def _check_path(path: str) -> None:
    if (
        not path.startswith("/api/v1/")
        or "//" in path
        or path.endswith("/")
        or ".." in path
        or "?" in path
        or "#" in path
        or "://" in path
        or any(c.isspace() for c in path)
    ):
        raise ValueError("path must be a plain /api/v1/... path")


def _pin(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS or parts.port:
        raise HostNotAllowed("only the Airwallex sandbox hosts may be called")
    return url


def _parse_expiry(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    if len(value) > 5 and value[-5] in "+-" and value[-4:].isdigit():
        value = value[:-2] + ":" + value[-2:]  # +0000 -> +00:00 for fromisoformat
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


class AirwallexClient:
    """Implements awx_common.AirwallexPort against the real sandbox."""

    def __init__(
        self,
        credentials: CredentialsProvider,
        *,
        timeout_s: float = 30.0,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        rate_limit_retries: int = 2,
        read_retries: int = 2,
    ) -> None:
        self._credentials = credentials
        self._timeout_s = timeout_s
        self._transport = transport or UrllibTransport()
        self._clock = clock
        self._sleep = sleep
        self._rate_limit_retries = rate_limit_retries
        self._read_retries = read_retries
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._redactor = Redactor()

    def __repr__(self) -> str:
        return "AirwallexClient(sandbox)"

    # ---- auth -------------------------------------------------------------------------------

    def _login(self) -> str:
        try:
            creds = self._credentials.get()
        except CredentialsUnavailable as err:
            raise Unauthorized(str(err), code="credentials_unavailable") from None
        self._redactor.add(*creds.secrets())
        request = HttpRequest(
            "POST",
            _pin(SANDBOX_API_BASE + LOGIN_PATH),
            {
                "x-client-id": creds.client_id,
                "x-api-key": creds.api_key,
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            b"",
            self._timeout_s,
        )
        try:
            response = self._transport.send(request)
        except TransportError as err:
            raise TransientServerError(
                self._redactor.text(f"login failed before a response: {err}")
            ) from None
        body = self._decode(response)
        if response.status != 200 and response.status != 201:
            err = error_from_response("GET", response.status, body, self._redactor)
            if response.status in (401, 403):
                raise Unauthorized(
                    f"sandbox login refused (HTTP {response.status})",
                    code=err.code,
                    http_status=response.status,
                ) from None
            raise err
        token = body.get("token") if isinstance(body, Mapping) else None
        if not isinstance(token, str) or not token:
            raise Unauthorized("sandbox login returned no token", http_status=response.status)
        self._redactor.add(token)
        expires = _parse_expiry(body.get("expires_at"))
        self._token = token
        self._token_expires_at = expires or (self._clock() + DEFAULT_TOKEN_LIFETIME_S)
        log.info("sandbox login ok; token valid %.0fs", self._token_expires_at - self._clock())
        return token

    def _bearer(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            fresh = self._token and self._token_expires_at - self._clock() > TOKEN_REFRESH_MARGIN_S
            if force_refresh or not fresh:
                return self._login()
            assert self._token is not None
            return self._token

    # ---- one HTTP exchange --------------------------------------------------------------------

    def _decode(self, response: HttpResponse) -> Any:
        try:
            return decode_json(response.body)
        except ValueError:
            return {"message": f"non-JSON response (HTTP {response.status})"}

    def _send(
        self,
        method: str,
        url: str,
        body: bytes | None,
        *,
        content_type: str | None = "application/json",
        on_behalf_of: str | None = None,
    ) -> HttpResponse:
        """Send with auth; refresh the token once on 401. TransportError propagates.

        x-on-behalf-of is sent whenever on_behalf_of is not None, and a value that is not an
        account id (`awx_common.tenancy.is_account_id`, `^acct_[A-Za-z0-9_-]+$`) raises before
        any HTTP call, login included: a malformed value is never downgraded to a
        platform-scope call, and no padding or control character can reach a header."""
        _pin(url)
        if on_behalf_of is not None and not is_account_id(on_behalf_of):
            raise TenantScopeViolation("refusing a malformed x-on-behalf-of; nothing was sent")
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self._bearer(force_refresh=attempt == 1)}"}
            headers["User-Agent"] = USER_AGENT
            if content_type:
                headers["Content-Type"] = content_type
            if on_behalf_of is not None:
                headers["x-on-behalf-of"] = on_behalf_of
            started = self._clock()
            response = self._transport.send(
                HttpRequest(method, url, headers, body, self._timeout_s)
            )
            log.info(
                "%s %s -> %s (%.2fs)%s",
                method,
                urlsplit(url).path,
                response.status,
                self._clock() - started,
                " on-behalf-of" if on_behalf_of is not None else "",
            )
            if response.status == 401 and attempt == 0:
                continue
            return response
        raise AssertionError("unreachable")

    # ---- AirwallexPort ------------------------------------------------------------------------

    def execute(self, op: Operation) -> OperationResult:
        if op.on_behalf_of is not None and op.path in PLATFORM_BODY_FIELDS:
            # Sandbox, 6 Oct: connected_account_transfers/create with x-on-behalf-of moved money
            # OUT of that connected account (connected-to-connected), and charges/create refused
            # it. These calls name the account in the body (awx_common.bind_platform_operation).
            return result_from_error(
                op,
                TenantScopeViolation(
                    f"{op.name} names its connected account in the body; refusing x-on-behalf-of"
                ),
            )
        try:
            _check_path(op.path)
            payload = encode_json(op.body)
        except (TypeError, ValueError) as err:
            raise ValueError(f"operation {op.name} is not sendable: {err}") from None
        url = SANDBOX_API_BASE + op.path
        for attempt in range(self._rate_limit_retries + 1):
            try:
                response = self._send(op.method, url, payload, on_behalf_of=op.on_behalf_of)
            except AwxError as err:  # login failed; nothing was sent
                return result_from_error(op, err)
            except TransportError as err:
                if err.sent:
                    unknown = OutcomeUnknown(
                        f"{op.name}: no response after the request was sent ({err}); "
                        "read it back by request_id before doing anything else",
                    )
                    return result_from_error(op, unknown)
                return result_from_error(op, TransientServerError(f"{op.name}: not sent ({err})"))
            body = self._decode(response)
            result = result_from_response(
                op, response.status, body, self._redactor, headers=response.headers
            )
            if response.status == 429 and attempt < self._rate_limit_retries:
                self._sleep(1.5 * (attempt + 1))
                continue
            if result.status is not OperationStatus.SUCCEEDED:
                log.info(
                    "%s %s: %s %s", op.name, op.request_id, result.error_type, result.error_code
                )
            return result
        raise AssertionError("unreachable")

    def read(self, req: ReadRequest, *, retries: int | None = None) -> Mapping[str, Any]:
        """GET with up to `retries` retries of a retryable failure (default: the client's
        read_retries). A caller that retries itself (the engine) can pass a lower count."""
        if retries is not None and retries < 0:
            raise ValueError("retries must be >= 0")
        _check_path(req.path)
        url = SANDBOX_API_BASE + req.path
        if req.query:
            url += "?" + urlencode(sorted(req.query.items()))
        last: AwxError | None = None
        for attempt in range((self._read_retries if retries is None else retries) + 1):
            if attempt:
                self._sleep(1.0 * attempt)
            try:
                response = self._send(
                    "GET", url, None, content_type=None, on_behalf_of=req.on_behalf_of
                )
            except TransportError as err:
                last = TransientServerError(self._redactor.text(f"GET {req.path} failed: {err}"))
                continue
            body = self._decode(response)
            if 200 <= response.status < 300:
                if isinstance(body, list):
                    return {"items": body}
                return body if isinstance(body, Mapping) else {}
            last = error_from_response(
                "GET", response.status, body, self._redactor, headers=response.headers
            )
            if not last.retryable:
                raise last
        assert last is not None
        raise last

    def upload(
        self,
        data: bytes,
        *,
        file_name: str,
        content_type: str,
        notes: str | None = None,
        on_behalf_of: str | None = None,
    ) -> Mapping[str, Any]:
        """Multipart POST /api/v1/files/upload on the files host (awx_common FileUploadPort).

        Sandbox, 6 Oct: 201 {"file_id", "filename", "notes", "object_type": "file", "size",
        "created"}. A resend after an unknown outcome only creates a second, inert file.
        """
        if not file_name or any(c in file_name for c in '"\r\n/\\'):
            raise ValueError("file_name must be a bare file name")
        boundary = uuid.uuid4().hex
        parts: list[bytes] = []
        if notes:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="notes"\r\n\r\n'
                f"{notes}\r\n".encode()
            )
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{file_name}"\r\nContent-Type: {content_type}\r\n\r\n'
            ).encode()
        )
        parts.append(bytes(data))
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        try:
            response = self._send(
                "POST",
                SANDBOX_FILES_BASE + "/api/v1/files/upload",
                b"".join(parts),
                content_type=f"multipart/form-data; boundary={boundary}",
                on_behalf_of=on_behalf_of,
            )
        except TransportError as err:
            if not err.sent:
                raise TransientServerError(f"file upload: not sent ({err})") from None
            raise OutcomeUnknown(f"file upload: no response ({err})") from None
        body = self._decode(response)
        if 200 <= response.status < 300 and isinstance(body, Mapping):
            return body
        raise error_from_response(
            "POST", response.status, body, self._redactor, headers=response.headers
        )
