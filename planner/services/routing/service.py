"""Get a route with as few external calls as possible: the RouteCache table first, then
each configured provider in turn.

A repeated request makes 0 calls and a cold one normally makes 1. When a provider fails,
the next one is tried instead of retrying the same one, which keeps latency predictable,
and a provider that keeps failing is skipped for a while by its circuit breaker.
"""

import hashlib
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models import F

from planner.models import RouteCache
from planner.services.routing.base import (
    LatLon,
    NoRouteError,
    ProviderError,
    RoutingProvider,
    RoutingUnavailable,
)
from planner.services.routing.ors import OrsProvider
from planner.services.routing.osrm import OsrmProvider
from planner.services.routing.resilience import OPEN, CircuitBreaker, OutboundThrottle

# 4 decimal places is about 11 m: points closer than that share one cached route.
KEY_DECIMALS = 4


@dataclass(frozen=True)
class RouteLookup:
    route: RouteCache
    cache_hit: bool
    external_calls: int  # routing requests sent for this lookup (0 on a cache hit)


def route_cache_key(start: LatLon, end: LatLon) -> str:
    (lat1, lon1), (lat2, lon2) = start, end
    d = KEY_DECIMALS
    text = f"driving|{lat1:.{d}f},{lon1:.{d}f}|{lat2:.{d}f},{lon2:.{d}f}"
    return hashlib.sha256(text.encode()).hexdigest()


def build_providers() -> list[RoutingProvider]:
    """The providers named in settings.ROUTING_PROVIDERS, in that order, leaving out any
    that are not set up (ORS runs only with ORS_API_KEY)."""
    factories = {"osrm": _osrm, "ors": _ors}
    unknown = [name for name in settings.ROUTING_PROVIDERS if name not in factories]
    if unknown:
        raise ImproperlyConfigured(f"Unknown routing provider(s) in ROUTING_PROVIDERS: {unknown}")
    built = (factories[name]() for name in settings.ROUTING_PROVIDERS)
    return [provider for provider in built if provider is not None]


def _osrm() -> OsrmProvider:
    throttle = OutboundThrottle("osrm", settings.OSRM_MIN_INTERVAL_SECONDS)
    return OsrmProvider(settings.OSRM_BASE_URL, settings.ROUTING_TIMEOUT_SECONDS, throttle)


def _ors() -> OrsProvider | None:
    if not settings.ORS_API_KEY:
        return None
    return OrsProvider(
        settings.ORS_BASE_URL,
        settings.ORS_API_KEY,
        settings.ORS_PROFILE,
        settings.ROUTING_TIMEOUT_SECONDS,
    )


def breaker_for(provider_name: str) -> CircuitBreaker:
    return CircuitBreaker(
        provider_name, settings.BREAKER_FAILURE_THRESHOLD, settings.BREAKER_RESET_SECONDS
    )


def provider_states() -> dict[str, str]:
    """For /health: each configured provider's breaker state, or "disabled"."""
    enabled = {provider.name for provider in build_providers()}
    return {
        name: breaker_for(name).state() if name in enabled else "disabled"
        for name in settings.ROUTING_PROVIDERS
    }


class RoutingService:
    def __init__(self, providers: list[RoutingProvider] | None = None):
        self.providers = build_providers() if providers is None else providers
        self.breakers = {provider.name: breaker_for(provider.name) for provider in self.providers}

    def get_route(self, start: LatLon, end: LatLon) -> RouteLookup:
        """Return the stored route for this start/finish pair, or fetch and store it.

        A provider's NoRouteError propagates untouched: it is an answer, not a failure.
        Raises RoutingUnavailable when every provider failed or was skipped.
        """
        # Route the rounded points, so a stored route is exactly the route for its key.
        start, end = _rounded(start), _rounded(end)
        key = route_cache_key(start, end)

        cached = RouteCache.objects.filter(key=key).first()
        if cached is not None:
            RouteCache.objects.filter(pk=cached.pk).update(hits=F("hits") + 1)
            return RouteLookup(cached, cache_hit=True, external_calls=0)

        calls = 0
        failures: list[str] = []
        for provider in self.providers:
            breaker = self.breakers[provider.name]
            if not breaker.allow_request():
                failures.append(f"{provider.name}: skipped, circuit breaker open")
                continue

            calls += 1
            try:
                result = provider.route(start, end)
            except NoRouteError:
                breaker.record_success()  # a real answer: the provider is working
                raise
            except ProviderError as exc:
                failures.append(str(exc))
                if exc.unhealthy:
                    breaker.record_failure()
                else:
                    breaker.record_success()  # it answered, it just refused this request
                continue

            breaker.record_success()
            # get_or_create, because a concurrent identical request may have stored it first.
            route, _ = RouteCache.objects.get_or_create(
                key=key,
                defaults={
                    "provider": result.provider,
                    "distance_m": result.distance_m,
                    "duration_s": result.duration_s,
                    "polyline6": result.polyline6,
                },
            )
            return RouteLookup(route, cache_hit=False, external_calls=calls)

        raise RoutingUnavailable(
            "No routing provider could answer. Please retry shortly.",
            self._retry_after_seconds(),
            failures,
        )

    def _retry_after_seconds(self) -> int:
        """When the first skipped provider is tried again. If none is skipped yet (they
        failed, but not often enough to open a breaker), one breaker reset period."""
        waits = [b.seconds_until_half_open() for b in self.breakers.values() if b.state() == OPEN]
        return max(1, min(waits, default=settings.BREAKER_RESET_SECONDS))


def _rounded(point: LatLon) -> LatLon:
    lat, lon = point
    return round(lat, KEY_DECIMALS), round(lon, KEY_DECIMALS)
