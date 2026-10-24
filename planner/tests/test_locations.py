import pytest
import responses
from responses import matchers

from planner.models import Place
from planner.services.locations import (
    GeocodingUnavailable,
    LocationNotFound,
    LocationOutsideUSA,
    resolve_location,
)

pytestmark = pytest.mark.django_db

NOMINATIM = "https://nominatim.example/search"


@pytest.fixture
def places():
    def town(name, key, state, lat, lon, country="US"):
        return Place(
            name=name,
            name_key=key,
            state=state,
            country=country,
            latitude=lat,
            longitude=lon,
            source="gazetteer",
        )

    Place.objects.bulk_create(
        [
            town("New York", "NEW YORK", "NY", 40.74838, -73.996705),
            # Same name, two counties, 7 mi apart: averaged, as for stations.
            town("Saint Louis", "ST LOUIS", "MO", 38.64, -90.32),
            town("Saint Louis", "ST LOUIS", "MO", 38.63, -90.19),
        ]
    )


@pytest.fixture
def nominatim(settings):
    """Mocked Nominatim. With nothing registered, any HTTP request fails the test."""
    settings.NOMINATIM_BASE_URL = "https://nominatim.example"
    with responses.RequestsMock() as mock:
        yield mock


@pytest.mark.parametrize("text", ["New York, NY", "new york, new york", "  New   York ,  ny "])
def test_city_and_state_resolve_offline(places, nominatim, text):
    where = resolve_location(text)

    assert (where.label, where.lat, where.lon) == ("New York, NY", 40.74838, -73.996705)
    assert (where.resolved_by, where.external_calls) == ("gazetteer", 0)


def test_same_name_towns_close_together_are_averaged(places, nominatim):
    where = resolve_location("St. Louis, MO")

    assert where.label == "Saint Louis, MO"
    assert (where.lat, where.lon) == pytest.approx((38.635, -90.255))


def test_coordinates_as_text_or_as_an_object_need_no_lookup(nominatim):
    text = resolve_location(" 40.7128 , -74.0060 ")
    obj = resolve_location({"lat": 40.7128, "lon": -74.006})

    assert (text.lat, text.lon, text.resolved_by) == (40.7128, -74.006, "coordinates")
    assert (obj.query, obj.label, obj.resolved_by) == (
        {"lat": 40.7128, "lon": -74.006},
        "40.71280, -74.00600",
        "coordinates",
    )


@pytest.mark.parametrize("value", ["48.8566,2.3522", {"lat": 19.4326, "lon": -99.1332}])
def test_points_outside_the_usa_are_rejected(nominatim, value):
    with pytest.raises(LocationOutsideUSA):
        resolve_location(value)


@pytest.mark.parametrize("text", ["61.2181,-149.9003", "21.3069,-157.8583"])  # Anchorage, Honolulu
def test_alaska_and_hawaii_are_in_the_usa(nominatim, text):
    assert resolve_location(text).resolved_by == "coordinates"


def test_unknown_city_goes_to_nominatim_once_then_comes_from_the_cache(places, nominatim, settings):
    nominatim.get(
        NOMINATIM,
        match=[
            matchers.query_param_matcher(
                {"q": "Pleasantville, NY", "countrycodes": "us", "format": "jsonv2", "limit": "1"}
            ),
            matchers.header_matcher({"User-Agent": settings.HTTP_USER_AGENT}),
        ],
        json=[{"lat": "41.1334", "lon": "-73.7921", "display_name": "Pleasantville, New York"}],
    )

    first = resolve_location("Pleasantville, NY")
    again = resolve_location("pleasantville,   NY")  # the same text once normalised

    assert len(nominatim.calls) == 1
    assert (first.label, first.resolved_by, first.external_calls) == (
        "Pleasantville, New York",
        "nominatim",
        1,
    )
    assert (again.lat, again.lon, again.external_calls) == (41.1334, -73.7921, 0)


def test_no_match_is_location_not_found_and_is_cached_too(nominatim):
    nominatim.get(NOMINATIM, json=[])

    for _ in range(2):
        with pytest.raises(LocationNotFound):
            resolve_location("zzzz qqqq")

    assert len(nominatim.calls) == 1


def test_geocoder_failure_is_reported_and_not_cached(nominatim):
    nominatim.get(NOMINATIM, status=503)
    nominatim.get(NOMINATIM, json=[{"lat": "41.0", "lon": "-73.0", "display_name": "Somewhere"}])

    with pytest.raises(GeocodingUnavailable):
        resolve_location("1 Main Street, Somewhere")
    assert resolve_location("1 Main Street, Somewhere").label == "Somewhere"
