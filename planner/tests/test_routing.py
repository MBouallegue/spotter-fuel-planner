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
from planner.services.routing.ors import OrsProvider
from planner.services.routing.osrm import OsrmProvider
from planner.services.routing.service import (
    RoutingService,
    breaker_for,
    build_providers,
    provider_states,
    route_cache_key,
)

OSRM = "https://osrm.example"
ORS = "https://ors.example"
START, END = (40.7484, -73.9857), (40.7851, -73.9680)  # Midtown to the Upper West Side
ROUTE_URL = f"{OSRM}/route/v1/driving/-73.985700,40.748400;-73.968000,40.785100"
ORS_URL = f"{ORS}/v2/directions/driving-car/geojson"


@pytest.fixture(autouse=True)
def routing_settings(settings):
    """Pin every routing setting, so a local .env (an ORS key, say) cannot leak in."""
    settings.ROUTING_PROVIDERS = ["osrm", "ors"]
    settings.OSRM_BASE_URL = OSRM
    settings.OSRM_MIN_INTERVAL_SECONDS = 0
    settings.ORS_BASE_URL = ORS
    settings.ORS_API_KEY = ""
    settings.ORS_PROFILE = "driving-car"
    settings.BREAKER_FAILURE_THRESHOLD = 3
    settings.BREAKER_RESET_SECONDS = 60


@pytest.fixture
def mock_http():
    with responses.RequestsMock() as mock:
        yield mock


@pytest.fixture
def osrm_answer(fixtures_dir):
    """A real router.project-osrm.org response for START -> END."""
    return json.loads((fixtures_dir / "osrm_route.json").read_text())


@pytest.fixture
def ors_answer(fixtures_dir):
    """An ORS v2 GeoJSON response for the same trip. Built in the documented shape from
    the OSRM line, as no key was at hand to record a live one."""
    return json.loads((fixtures_dir / "ors_route.json").read_text())


def osrm() -> OsrmProvider:
    return OsrmProvider(OSRM, timeout=(3, 15))


def ors() -> OrsProvider:
    return OrsProvider(ORS, "test-key", "driving-car", timeout=(3, 15))


def elsewhere(i: int) -> tuple[float, float]:
    """A different finish per request, so no request is answered from the route cache."""
    return END[0] + i / 100, END[1]


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
    ("reply", "unhealthy"),
    [
        ({"status": 503, "body": "<html>Service Unavailable</html>"}, True),
        ({"status": 429, "json": {"message": "Too Many Requests"}}, True),
        ({"status": 200, "json": {"code": "Ok", "routes": []}}, True),
        ({"status": 200, "json": {"code": "Ok", "routes": [{"distance": 1, "duration": 1}]}}, True),
        ({"body": requests.ConnectTimeout("timed out")}, True),
        ({"status": 400, "json": {"code": "InvalidQuery", "message": "Bad query"}}, False),
    ],
    ids=["5xx-html", "429", "no-routes", "no-geometry", "timeout", "refused"],
)
def test_osrm_failures_say_whether_the_provider_is_in_trouble(mock_http, reply, unhealthy):
    mock_http.get(ROUTE_URL, **reply)

    with pytest.raises(ProviderError) as exc:
        osrm().route(START, END)

    assert exc.value.unhealthy is unhealthy


# --- ORS provider -------------------------------------------------------------------------


def test_ors_parses_its_geojson_and_reencodes_the_line(mock_http, ors_answer, osrm_answer):
    mock_http.post(
        ORS_URL,
        match=[
            # [lon, lat] pairs, and no turn-by-turn instructions.
            matchers.json_params_matcher(
                {"coordinates": [[-73.9857, 40.7484], [-73.968, 40.7851]], "instructions": False}
            ),
            matchers.header_matcher({"Authorization": "test-key"}),
        ],
        json=ors_answer,
    )

    result = ors().route(START, END)

    assert (result.provider, result.distance_m, result.duration_s) == ("ors", 6588.4, 702.3)
    # Stored as a precision-6 polyline, the line matches the one OSRM drew point for point.
    osrm_points = polyline.decode(osrm_answer["routes"][0]["geometry"], 6)
    assert polyline.decode(result.polyline6, 6) == osrm_points


@pytest.mark.parametrize("code", [2009, 2010])  # route not found, point not found
def test_ors_no_route_codes_are_answers(mock_http, code):
    mock_http.post(ORS_URL, status=404, json={"error": {"code": code, "message": "Not found"}})

    with pytest.raises(NoRouteError):
        ors().route(START, END)


@pytest.mark.parametrize(
    ("reply", "unhealthy"),
    [
        ({"status": 502, "body": "<html>Bad Gateway</html>"}, True),
        ({"status": 429, "json": {"error": "Rate limit exceeded"}}, True),
        ({"status": 200, "json": {"type": "FeatureCollection", "features": []}}, True),
        ({"status": 400, "json": {"error": {"code": 2004, "message": "Over 6000000.0 m"}}}, False),
        ({"status": 403, "json": {"error": "Access to this API has been disallowed"}}, False),
    ],
    ids=["5xx-html", "429", "no-features", "over-6000-km", "bad-key"],
)
def test_ors_failures_say_whether_the_provider_is_in_trouble(mock_http, reply, unhealthy):
    mock_http.post(ORS_URL, **reply)

    with pytest.raises(ProviderError) as exc:
        ors().route(START, END)

    assert exc.value.unhealthy is unhealthy


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
def test_osrm_down_hands_over_to_ors(mock_http, ors_answer, settings):
    settings.ORS_API_KEY = "test-key"
    mock_http.get(ROUTE_URL, status=502, body="<html>Bad Gateway</html>")
    mock_http.post(ORS_URL, json=ors_answer)

    lookup = RoutingService().get_route(START, END)

    assert (lookup.route.provider, lookup.external_calls) == ("ors", 2)


@pytest.mark.django_db
def test_a_provider_that_keeps_failing_is_skipped():
    failing, fallback = FakeProvider("osrm", ProviderError("osrm: HTTP 503")), FakeProvider("ors")

    # A new service per trip, as per request: the breaker's state lives in the cache.
    lookups = [RoutingService([failing, fallback]).get_route(START, elsewhere(i)) for i in range(4)]

    assert failing.calls == 3  # the third failure opened its breaker
    assert [lookup.external_calls for lookup in lookups] == [2, 2, 2, 1]
    assert fallback.calls == 4


@pytest.mark.django_db
def test_no_route_answers_never_trip_the_breaker():
    provider = FakeProvider("osrm", NoRouteError("no road"))
    fallback = FakeProvider("ors")

    for i in range(5):
        with pytest.raises(NoRouteError):
            RoutingService([provider, fallback]).get_route(START, elsewhere(i))

    assert provider.calls == 5
    assert fallback.calls == 0  # a real answer is never failed over
    assert breaker_for("osrm").state() == "closed"


@pytest.mark.django_db
def test_refusals_fail_over_but_never_trip_the_breaker():
    refusing = FakeProvider("ors", ProviderError("ors: HTTP 400, too long", unhealthy=False))

    for _ in range(5):
        with pytest.raises(RoutingUnavailable):
            RoutingService([refusing]).get_route(START, END)

    assert breaker_for("ors").state() == "closed"


@pytest.mark.django_db
def test_every_provider_failing_means_routing_unavailable():
    failing = FakeProvider("osrm", ProviderError("osrm: ReadTimeout"))

    with pytest.raises(RoutingUnavailable) as exc:
        RoutingService([failing]).get_route(START, END)

    assert exc.value.retry_after_seconds == 60  # no breaker open yet: one reset period
    assert exc.value.details == {"failures": ["osrm: ReadTimeout"]}


@pytest.mark.django_db
def test_with_every_breaker_open_retry_after_is_the_time_left():
    failing = FakeProvider("osrm", ProviderError("osrm: HTTP 503"))
    for _ in range(3):
        with pytest.raises(RoutingUnavailable):
            RoutingService([failing]).get_route(START, END)

    with pytest.raises(RoutingUnavailable) as exc:
        RoutingService([failing]).get_route(START, END)

    assert failing.calls == 3  # skipped this time
    assert 0 < exc.value.retry_after_seconds <= 60
    assert exc.value.details == {"failures": ["osrm: skipped, circuit breaker open"]}


def test_cache_key_rounds_to_4_decimals_and_keeps_direction():
    assert route_cache_key((40.74841, -73.98569), END) == route_cache_key(START, END)
    assert route_cache_key(START, END) != route_cache_key(END, START)


def test_providers_come_from_settings_and_ors_needs_a_key(settings):
    assert [p.name for p in build_providers()] == ["osrm"]

    settings.ORS_API_KEY = "test-key"
    assert [p.name for p in build_providers()] == ["osrm", "ors"]

    settings.ROUTING_PROVIDERS = ["osrm", "carrier-pigeon"]
    with pytest.raises(ImproperlyConfigured, match="carrier-pigeon"):
        build_providers()


def test_provider_states_show_breakers_and_disabled_providers(settings):
    assert provider_states() == {"osrm": "closed", "ors": "disabled"}

    settings.ORS_API_KEY = "test-key"
    for _ in range(3):
        breaker_for("osrm").record_failure()

    assert provider_states() == {"osrm": "open", "ors": "closed"}
