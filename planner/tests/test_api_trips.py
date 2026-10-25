"""The trip endpoint end to end, with OSRM and Nominatim mocked.

The test world: two towns 10 degrees apart on the 35th parallel, joined by a 600-mile
road. Three stations sit along it at miles 120, 240 and 360, and a cheaper one sits
20 miles off the road, outside the corridor.
"""

from decimal import ROUND_HALF_UP, Decimal

import numpy as np
import polyline
import pytest
import responses
from rest_framework.throttling import AnonRateThrottle

from planner.models import METERS_PER_MILE, FuelStation, Place

pytestmark = pytest.mark.django_db

URL = "/api/v1/trips/plan/"
OSRM = "https://osrm.example"
ORS = "https://ors.example"
ROUTE_URL = f"{OSRM}/route/v1/driving/-101.000000,35.000000;-91.000000,35.000000"
TRIP = {"start": "Start Town, TX", "finish": "Finish Town, AR"}


def road() -> dict:
    """An OSRM answer: a straight road along the parallel, 600 miles by the odometer."""
    points = [(35.0, lon) for lon in np.linspace(-101.0, -91.0, 201)]
    return {
        "code": "Ok",
        "routes": [
            {
                "distance": 600 * METERS_PER_MILE,
                "duration": 10 * 3600,
                "geometry": polyline.encode(points, 6),
            }
        ],
    }


def town(name: str, state: str, lat: float, lon: float) -> Place:
    return Place(
        name=name,
        name_key=name.upper(),
        state=state,
        latitude=lat,
        longitude=lon,
        source="gazetteer",
    )


def station(opis_id: int, name: str, lat: float, lon: float, price: str) -> FuelStation:
    return FuelStation(
        opis_id=opis_id,
        name=name,
        address="I-40",
        city="Somewhere",
        state="OK",
        country="US",
        latitude=lat,
        longitude=lon,
        price_per_gallon=Decimal(price),
        geocode_source="gazetteer",
    )


@pytest.fixture(autouse=True)
def world(db, settings):
    settings.ROUTING_PROVIDERS = ["osrm", "ors"]
    settings.OSRM_BASE_URL = OSRM
    settings.OSRM_MIN_INTERVAL_SECONDS = 0
    settings.ORS_BASE_URL = ORS
    settings.ORS_API_KEY = ""  # off unless a test turns it on
    settings.NOMINATIM_BASE_URL = "https://nominatim.example"
    Place.objects.bulk_create(
        [town("Start Town", "TX", 35.0, -101.0), town("Finish Town", "AR", 35.0, -91.0)]
    )
    FuelStation.objects.bulk_create(
        [
            station(1, "FIRST", 35.0, -99.0, "3.500"),  # mile 120
            station(2, "CHEAP", 35.02, -97.0, "3.100"),  # mile 240, 1.4 mi off the road
            station(3, "LAST", 35.0, -95.0, "3.400"),  # mile 360
            station(4, "TOO FAR", 35.3, -96.0, "2.000"),  # 20 mi off the road
        ]
    )


@pytest.fixture
def http():
    """Mocked outbound HTTP; a request with no registered answer fails the test."""
    with responses.RequestsMock() as mock:
        yield mock


def post(client, payload: dict):
    return client.post(URL, payload, content_type="application/json")


def error_of(response) -> dict:
    """Check the one error shape every failure shares, and return its inner object."""
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    return body["error"]


def test_plans_the_cheapest_stops_and_reports_everything(client, http):
    http.get(ROUTE_URL, json=road())

    response = post(client, TRIP)

    assert response.status_code == 200
    body = response.json()
    assert body["start"] == {
        "query": "Start Town, TX",
        "label": "Start Town, TX",
        "lat": 35.0,
        "lon": -101.0,
        "resolved_by": "gazetteer",
    }
    assert body["route"] == {"distance_miles": 600.0, "duration_hours": 10.0, "provider": "osrm"}
    assert body["vehicle"] == {
        "max_range_miles": 500.0,
        "mpg": 10.0,
        "tank_gallons": 50.0,
        "start_fuel_gallons": 50.0,
    }

    # 60 gal needed, 50 in the tank: the 10 missing gallons come from the cheapest station
    # in the corridor. The $2.00 station is 20 mi off the road, so it is never offered.
    [stop] = body["fuel_stops"]
    assert (stop["sequence"], stop["opis_id"], stop["name"]) == (1, 2, "CHEAP")
    assert (stop["gallons"], stop["price_per_gallon"], stop["cost"]) == (10.0, 3.1, 31.0)
    assert stop["mile_marker"] == pytest.approx(240, abs=0.5)
    assert stop["miles_from_previous"] == stop["mile_marker"]
    assert stop["fuel_on_arrival_gallons"] == pytest.approx(26.0, abs=0.05)
    assert stop["distance_from_route_miles"] == pytest.approx(1.4, abs=0.1)

    summary = body["summary"]
    assert summary["total_fuel_cost"] == sum(s["cost"] for s in body["fuel_stops"]) == 31.0
    assert (summary["stops"], summary["fuel_used_gallons"], summary["fuel_purchased_gallons"]) == (
        1,
        60.0,
        10.0,
    )
    assert (summary["end_fuel_gallons"], summary["average_price_paid"]) == (0.0, 3.1)
    assert summary["last_leg_miles"] == pytest.approx(360, abs=0.5)
    assert summary["note"].startswith("Vehicle starts with a full tank")

    assert body["meta"]["external_calls"] == {"routing": 1, "geocoding": 0}
    assert body["meta"]["route_cache"] == "miss"
    assert body["meta"]["candidate_stations"] == 3
    assert set(body["meta"]["timing_ms"]) == {"total", "geocoding", "routing", "planning"}


def test_map_is_geojson_with_lon_lat_positions(client, http):
    http.get(ROUTE_URL, json=road())

    geojson = post(client, TRIP).json()["map"]

    assert geojson["type"] == "FeatureCollection"
    kinds = [f["properties"]["kind"] for f in geojson["features"]]
    assert kinds == ["route", "start", "finish", "fuel_stop"]
    route, start, _, stop = geojson["features"]
    assert route["geometry"]["type"] == "LineString"
    # A straight road simplifies to its two ends, written [lon, lat].
    assert route["geometry"]["coordinates"] == [[-101.0, 35.0], [-91.0, 35.0]]
    assert start["geometry"] == {"type": "Point", "coordinates": [-101.0, 35.0]}
    assert (stop["properties"]["name"], stop["properties"]["cost"]) == ("CHEAP", 31.0)


def test_a_repeated_request_makes_no_routing_call(client, http):
    http.get(ROUTE_URL, json=road())

    post(client, TRIP)
    again = post(client, TRIP).json()

    assert len(http.calls) == 1
    assert again["meta"]["route_cache"] == "hit"
    assert again["meta"]["external_calls"] == {"routing": 0, "geocoding": 0}
    assert again["summary"]["total_fuel_cost"] == 31.0


def test_get_alias_takes_the_same_fields_as_query_parameters(client, http):
    http.get(ROUTE_URL, json=road())

    response = client.get(URL, {**TRIP, "start_fuel_gallons": "50", "corridor_miles": "5"})

    assert response.status_code == 200
    assert response.json()["summary"]["total_fuel_cost"] == 31.0


def test_a_low_start_tank_buys_more_on_the_way(client, http):
    http.get(ROUTE_URL, json=road())

    body = post(client, {**TRIP, "start_fuel_gallons": 15}).json()

    # 15 gal reach mile 150, so the $3.50 station is a must; it sells just enough to get
    # to the $3.10 one, which sells the rest. Mile markers are exact to a quarter mile,
    # hence the tolerance on gallons.
    stops = body["fuel_stops"]
    assert [s["opis_id"] for s in stops] == [1, 2]
    assert [s["gallons"] for s in stops] == pytest.approx([9.0, 36.0], abs=0.05)
    assert body["summary"]["fuel_purchased_gallons"] == pytest.approx(45.0, abs=0.002)
    # The total is exactly the sum of the stop costs, each priced from the gallons shown.
    for s in stops:
        assert Decimal(str(s["cost"])) == (
            Decimal(str(s["gallons"])) * Decimal(str(s["price_per_gallon"]))
        ).quantize(Decimal("0.01"), ROUND_HALF_UP)
    total = Decimal(str(body["summary"]["total_fuel_cost"]))
    assert total == sum(Decimal(str(s["cost"])) for s in stops)
    assert body["summary"]["note"].startswith("Vehicle starts with 15 of 50 gallons")


def test_a_gap_longer_than_the_range_is_422_with_where_it_happens(client, http):
    http.get(ROUTE_URL, json=road())

    response = post(client, {**TRIP, "max_range_miles": 150})

    # Stations at 120, 240 and 360, the finish at 600: the last 240 miles cannot be driven.
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "no_feasible_plan"
    details = error["details"]
    assert details["stuck_at_mile"] == pytest.approx(360, abs=0.5)
    assert details["next_point_mile"] == 600.0
    assert details["gap_miles"] == pytest.approx(240, abs=0.5)
    assert details["stuck_at"] == pytest.approx({"lat": 35.0, "lon": -95.0}, abs=0.01)


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"start": "Start Town, TX"}, "finish"),
        ({**TRIP, "start": "   "}, "start"),
        ({**TRIP, "start": 42}, "start"),
        ({**TRIP, "start": {"lat": 95, "lon": 0}}, "start"),
        ({**TRIP, "mpg": 0}, "mpg"),
        ({**TRIP, "max_range_miles": "far"}, "max_range_miles"),
        ({**TRIP, "corridor_miles": 30}, "corridor_miles"),
        ({**TRIP, "start_fuel_gallons": 60}, "start_fuel_gallons"),
    ],
)
def test_invalid_input_is_400_with_the_field_errors(client, payload, field):
    response = post(client, payload)

    assert response.status_code == 400
    error = error_of(response)
    assert error["code"] == "invalid_request"
    assert field in error["details"]


def test_malformed_json_is_400(client):
    response = client.post(URL, "{not json", content_type="application/json")

    assert response.status_code == 400
    assert error_of(response)["code"] == "invalid_request"


def test_start_and_finish_less_than_a_mile_apart_is_400(client):
    response = post(client, {"start": "Start Town, TX", "finish": "35.001,-101.001"})

    assert response.status_code == 400
    assert error_of(response)["code"] == "same_start_and_finish"


def test_unknown_place_is_422(client, http):
    http.get("https://nominatim.example/search", json=[])

    response = post(client, {**TRIP, "finish": "Atlantis, TX"})

    assert response.status_code == 422
    assert error_of(response)["code"] == "location_not_found"


def test_geocoder_down_is_503_with_retry_after(client, http):
    http.get("https://nominatim.example/search", status=503)

    response = post(client, {**TRIP, "finish": "1 Main Street, Little Rock"})

    assert response.status_code == 503
    assert int(response["Retry-After"]) > 0
    assert error_of(response)["code"] == "geocoding_unavailable"


def test_place_outside_the_usa_is_422(client):
    response = post(client, {**TRIP, "finish": {"lat": 48.8566, "lon": 2.3522}})

    assert response.status_code == 422
    assert error_of(response)["code"] == "location_outside_usa"


def test_no_road_between_the_points_is_422(client, http):
    http.get(ROUTE_URL, status=400, json={"code": "NoRoute", "message": "Impossible route"})

    response = post(client, TRIP)

    assert response.status_code == 422
    assert error_of(response)["code"] == "no_route"


def test_routing_down_is_503_with_retry_after(client, http):
    http.get(ROUTE_URL, status=502, body="<html>Bad Gateway</html>")

    response = post(client, TRIP)

    assert response.status_code == 503
    assert int(response["Retry-After"]) > 0
    error = error_of(response)
    assert error["code"] == "routing_unavailable"
    assert error["details"] == {"failures": ["osrm: HTTP 502, body is not JSON"]}


def test_with_osrm_down_ors_answers_and_the_trip_still_plans(client, http, settings):
    settings.ORS_API_KEY = "test-key"
    http.get(ROUTE_URL, status=502, body="<html>Bad Gateway</html>")
    points = polyline.decode(road()["routes"][0]["geometry"], 6)
    http.post(
        f"{ORS}/v2/directions/driving-car/geojson",
        json={
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "summary": {"distance": 600 * METERS_PER_MILE, "duration": 36000}
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[lon, lat] for lat, lon in points],
                    },
                }
            ],
        },
    )

    body = post(client, TRIP).json()

    assert body["route"]["provider"] == "ors"
    assert body["meta"]["external_calls"]["routing"] == 2
    assert body["summary"]["total_fuel_cost"] == 31.0


def test_too_many_requests_is_429_with_retry_after(client, monkeypatch):
    # DRF reads the rates once at import, so the test lowers them on the class itself.
    monkeypatch.setattr(AnonRateThrottle, "THROTTLE_RATES", {"anon": "2/min"})

    statuses = [client.get("/api/v1/health/").status_code for _ in range(3)]

    assert statuses == [200, 200, 429]
    response = client.get("/api/v1/health/")
    assert int(response["Retry-After"]) > 0
    error = error_of(response)
    assert error["code"] == "throttled"
    assert error["details"]["retry_after_seconds"] > 0


def test_other_http_errors_keep_the_same_shape(client):
    response = client.delete(URL)

    assert response.status_code == 405
    assert error_of(response)["code"] == "method_not_allowed"
