import json

import polyline
import pytest
import requests
import responses
from django.core.exceptions import ImproperlyConfigured
from responses import matchers

from planner.models import RouteCache
from planner.services.routing.base import (
    NoRouteError,
    ProviderError,
    RouteResult,
    RoutingUnavailable,
)
from planner.services.routing.osrm import OsrmProvider
from planner.services.routing.service import RoutingService, build_providers, route_cache_key

OSRM = "https://osrm.example"
START, END = (40.7484, -73.9857), (40.7851, -73.9680)  # Midtown to the Upper West Side
ROUTE_URL = f"{OSRM}/route/v1/driving/-73.985700,40.748400;-73.968000,40.785100"


@pytest.fixture
def mock_http():
    with responses.RequestsMock() as mock:
        yield mock


@pytest.fixture
def osrm_answer(fixtures_dir):
    """A real router.project-osrm.org response for START -> END."""
    return json.loads((fixtures_dir / "osrm_route.json").read_text())


def osrm() -> OsrmProvider:
    return OsrmProvider(OSRM, timeout=(3, 15))


class FakeProvider:
    def __init__(self, name: str = "fake", error: Exception | None = None):
        self.name = name
        self.error = error
        self.calls = 0

    def route(self, start, end) -> RouteResult:
        self.calls += 1
        if self.error:
            raise self.error
        return RouteResult(self.name, 6565.8, 680.2, polyline.encode([start, end], 6))


# --- OSRM provider ------------------------------------------------------------------------


def test_osrm_parses_a_real_response(mock_http, osrm_answer, settings, caplog):
    mock_http.get(
        ROUTE_URL,  # lon,lat order in the path
        match=[
            matchers.query_param_matcher(
                {
                    "overview": "full",
                    "geometries": "polyline6",
                    "steps": "false",
                    "alternatives": "false",
                    "annotations": "false",
                }
            ),
            matchers.header_matcher({"User-Agent": settings.HTTP_USER_AGENT}),
        ],
        json=osrm_answer,
    )

    with caplog.at_level("INFO", logger="planner"):
        result = osrm().route(START, END)

    assert result == RouteResult("osrm", 6565.8, 680.2, osrm_answer["routes"][0]["geometry"])
    assert "osrm responded HTTP 200" in caplog.text


def test_osrm_no_route_is_an_answer_not_a_failure(mock_http, fixtures_dir):
    body = json.loads((fixtures_dir / "osrm_no_route.json").read_text())
    mock_http.get(ROUTE_URL, status=400, json=body)  # the live server sends NoRoute as a 400

    with pytest.raises(NoRouteError, match="Impossible route"):
        osrm().route(START, END)


@pytest.mark.parametrize(
    "reply",
    [
        {"status": 503, "body": "<html>Service Unavailable</html>"},
        {"status": 429, "json": {"message": "Too Many Requests"}},
        {"status": 400, "json": {"code": "InvalidQuery", "message": "Query string malformed"}},
        {"status": 200, "json": {"code": "Ok", "routes": []}},
        {"status": 200, "json": {"code": "Ok", "routes": [{"distance": 1, "duration": 1}]}},
        {"body": requests.ConnectTimeout("timed out")},
    ],
    ids=["5xx-html", "429", "invalid-query", "no-routes", "no-geometry", "timeout"],
)
def test_osrm_failures_become_provider_errors(mock_http, reply):
    mock_http.get(ROUTE_URL, **reply)

    with pytest.raises(ProviderError):
        osrm().route(START, END)


# --- routing service ----------------------------------------------------------------------


@pytest.mark.django_db
def test_a_repeated_request_is_served_from_the_route_cache():
    provider = FakeProvider()
    service = RoutingService([provider])

    first = service.get_route(START, END)
    again = service.get_route((40.74841, -73.98569), END)  # the same point at 4 dp

    assert provider.calls == 1
    assert (first.cache_hit, first.external_calls) == (False, 1)
    assert (again.cache_hit, again.external_calls) == (True, 0)
    assert again.route.pk == first.route.pk
    assert RouteCache.objects.get().hits == 1


@pytest.mark.django_db
def test_a_failing_provider_hands_over_to_the_next():
    broken = FakeProvider("broken", ProviderError("broken: HTTP 503"))
    working = FakeProvider("working")

    lookup = RoutingService([broken, working]).get_route(START, END)

    assert (lookup.route.provider, lookup.external_calls) == ("working", 2)


@pytest.mark.django_db
def test_no_route_is_returned_at_once_without_failing_over():
    first, second = FakeProvider("first", NoRouteError("no road")), FakeProvider("second")

    with pytest.raises(NoRouteError):
        RoutingService([first, second]).get_route(START, END)

    assert second.calls == 0
    assert not RouteCache.objects.exists()


@pytest.mark.django_db
def test_every_provider_failing_means_routing_unavailable():
    failing = FakeProvider("osrm", ProviderError("osrm: ReadTimeout"))

    with pytest.raises(RoutingUnavailable) as exc:
        RoutingService([failing]).get_route(START, END)

    assert exc.value.retry_after_seconds > 0
    assert exc.value.details == {"failures": ["osrm: ReadTimeout"]}


def test_cache_key_rounds_to_4_decimals_and_keeps_direction():
    assert route_cache_key((40.74841, -73.98569), END) == route_cache_key(START, END)
    assert route_cache_key(START, END) != route_cache_key(END, START)


def test_providers_come_from_settings_in_order(settings):
    settings.ROUTING_PROVIDERS = ["osrm"]
    assert [p.name for p in build_providers()] == ["osrm"]

    settings.ROUTING_PROVIDERS = ["osrm", "carrier-pigeon"]
    with pytest.raises(ImproperlyConfigured, match="carrier-pigeon"):
        build_providers()
