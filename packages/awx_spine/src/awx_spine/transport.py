"""HTTP transport seam. The client talks to a Transport so unit tests never touch the network.

TransportError.sent says whether the request may have reached the provider. False means it
certainly did not (DNS failure, connection refused); True means it may have (timeout, reset after
the connection opened), so a write's outcome is unknown.
"""

from __future__ import annotations

import socket
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(repr=False)  # carries Authorization; never printed
    body: bytes | None = field(default=None, repr=False)
    timeout_s: float = 30.0


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes = field(repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)


class TransportError(Exception):
    def __init__(self, message: str, *, sent: bool) -> None:
        super().__init__(message)
        self.sent = sent


class Transport(Protocol):
    def send(self, request: HttpRequest) -> HttpResponse: ...


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect.

    urllib's default handler follows 3xx to any host the Location names, copying every header
    (Authorization, x-api-key, x-client-id) and turning a POST into a bodyless GET. Returning
    None makes urllib raise the 3xx as an HTTPError, which send() returns as a response; the
    client then treats it as a refused, non-retryable answer.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def build_opener(*handlers: urllib.request.BaseHandler) -> urllib.request.OpenerDirector:
    """An opener that refuses redirects. Extra handlers are for tests."""
    return urllib.request.build_opener(_RefuseRedirects(), *handlers)


class UrllibTransport:
    """Standard-library HTTPS. HTTP error statuses (3xx included) come back as responses."""

    def __init__(self, opener: urllib.request.OpenerDirector | None = None) -> None:
        self._opener = opener or build_opener()

    def send(self, request: HttpRequest) -> HttpResponse:
        req = urllib.request.Request(  # noqa: S310 - the client pins the sandbox https hosts
            request.url,
            data=request.body,
            method=request.method,
            headers=dict(request.headers),
        )
        try:
            with self._opener.open(req, timeout=request.timeout_s) as resp:
                return HttpResponse(resp.status, resp.read(), dict(resp.headers.items()))
        except urllib.error.HTTPError as err:
            body = err.read()
            return HttpResponse(err.code, body, dict(err.headers.items()) if err.headers else {})
        except TimeoutError:
            raise TransportError("timed out waiting for the provider", sent=True) from None
        except urllib.error.URLError as err:
            raise TransportError(_reason(err.reason), sent=_maybe_sent(err.reason)) from None
        except (ConnectionError, ssl.SSLError, OSError) as err:
            raise TransportError(type(err).__name__, sent=_maybe_sent(err)) from None


def _reason(reason: object) -> str:
    return f"connection failed ({type(reason).__name__})"


def _maybe_sent(reason: object) -> bool:
    """Only failures that happen before any byte leaves count as not sent."""
    if isinstance(reason, socket.gaierror | ConnectionRefusedError):
        return False
    return True
