"""The contract every routing provider follows, so the service can treat them all alike."""

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

LatLon = tuple[float, float]


@dataclass(frozen=True)
class RouteResult:
    provider: str
    distance_m: float
    duration_s: float
    polyline6: str  # full-resolution geometry, encoded polyline, precision 6


class RoutingProvider(Protocol):
    name: str

    def route(self, start: LatLon, end: LatLon) -> RouteResult: ...


class NoRouteError(Exception):
    """The provider worked and says no road connects the points. A real answer: the service
    must not fail over to another provider or count it as a provider failure."""


class ProviderError(Exception):
    """The provider gave no route, so the service tries the next one.

    ``unhealthy`` is True when the provider itself is in trouble (network error, timeout,
    5xx, 429, malformed body), which counts against its circuit breaker. It is False when
    the provider answered properly but refused this request, such as ORS's 6,000 km limit.
    """

    def __init__(self, message: str, unhealthy: bool = True):
        super().__init__(message)
        self.unhealthy = unhealthy


def is_provider_trouble(status: int) -> bool:
    """5xx and 429 mean the provider is struggling; other 4xx refuse this one request."""
    return status >= 500 or status == 429


class RoutingUnavailable(Exception):
    """No provider could answer, so the client should retry later."""

    def __init__(self, message: str, retry_after_seconds: int, failures: list[str]):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.details = {"failures": failures}


def request_json(
    provider: str, method: str, url: str, *, timeout: tuple[float, float], **kwargs
) -> tuple[int, Any]:
    """Send one request and return (HTTP status, decoded JSON body), whatever the status.

    Every call is logged with the provider, status and duration. A network error, a
    timeout or a body that is not JSON becomes ProviderError: the cue to fail over.
    """
    headers = {"User-Agent": settings.HTTP_USER_AGENT, **kwargs.pop("headers", {})}
    started = time.perf_counter()
    try:
        response = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        logger.warning("%s call failed after %d ms: %r", provider, _elapsed_ms(started), exc)
        raise ProviderError(f"{provider}: {type(exc).__name__}") from exc
    status = response.status_code
    logger.info("%s responded HTTP %s in %d ms", provider, status, _elapsed_ms(started))
    try:
        return status, response.json()
    except ValueError:
        raise ProviderError(f"{provider}: HTTP {status}, body is not JSON") from None


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000
