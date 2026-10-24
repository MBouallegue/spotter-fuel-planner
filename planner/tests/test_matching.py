from decimal import Decimal

import numpy as np
import pytest

from planner.models import FuelStation
from planner.services.geometry import EARTH_RADIUS_MILES, resample_line
from planner.services.matching import stations_along_route
from planner.services.station_index import StationIndex, StationInfo, get_station_index

MILES_PER_DEGREE = 2 * np.pi * EARTH_RADIUS_MILES / 360


def station(opis_id: int, lat: float, lon: float, price: str) -> StationInfo:
    return StationInfo(
        opis_id, f"STOP {opis_id}", "I-0", "Town", "XX", "US", lat, lon, Decimal(price)
    )


def north(miles: float) -> float:
    """The latitude that many miles north of the equator (negative: south)."""
    return miles / MILES_PER_DEGREE


@pytest.fixture
def equator_route():
    """10 degrees east along the equator (about 691 mi), resampled every half mile."""
    path = np.column_stack([np.zeros(11), np.linspace(0, 10, 11)])
    return resample_line(path, step_miles=0.5, total_miles=10 * MILES_PER_DEGREE)


def test_stations_get_the_mile_where_the_route_passes_them(equator_route):
    index = StationIndex.from_stations(
        [
            station(1, 0.0, 1.0, "3.50"),  # on the route
            station(2, north(3), 2.0, "3.40"),  # 3 mi north: inside the 5-mile corridor
            station(3, north(6), 3.0, "3.00"),  # 6 mi north: outside
            station(4, 0.0, 11.0, "2.00"),  # 69 mi past the finish: outside
            station(5, north(-4.5), 9.0, "3.20"),  # 4.5 mi south: inside
        ]
    )

    match = stations_along_route(equator_route, index, corridor_miles=5.0)

    assert [c.station_id for c in match.candidates] == [1, 2, 5]
    # Route points are half a mile apart, and both the mile marker and the distance are
    # measured to the nearest one, so each can be off by up to a quarter mile.
    assert [c.mile for c in match.candidates] == pytest.approx(
        [MILES_PER_DEGREE, 2 * MILES_PER_DEGREE, 9 * MILES_PER_DEGREE], abs=0.25
    )
    assert [c.price for c in match.candidates] == [3.5, 3.4, 3.2]
    assert match.distance_from_route == pytest.approx({1: 0.0, 2: 3.0, 5: 4.5}, abs=0.25)


def test_stations_in_one_town_collapse_to_the_cheapest(equator_route):
    index = StationIndex.from_stations(
        [
            station(10, 0.0, 5.0, "3.30"),
            station(12, 0.0, 5.0, "3.10"),
            station(11, 0.0, 5.0, "3.10"),  # same price as 12: the lower OPIS ID wins
            station(13, 0.0, 6.0, "3.90"),  # another town
        ]
    )

    match = stations_along_route(equator_route, index, corridor_miles=5.0)

    assert [c.station_id for c in match.candidates] == [11, 13]


def test_corridor_width_decides_what_counts_as_on_the_route(equator_route):
    index = StationIndex.from_stations([station(1, north(7), 4.0, "3.00")])

    assert stations_along_route(equator_route, index, corridor_miles=5.0).candidates == []
    assert len(stations_along_route(equator_route, index, corridor_miles=10.0).candidates) == 1


def test_bounding_box_widens_east_west_at_high_latitudes():
    # At 60N a degree of longitude is half as long, so 4 mi east of the route's end is
    # 0.116 degrees: outside a box padded by 5/69 degrees, inside one adjusted by cos(lat).
    path = np.array([[60.0, -150.0], [60.0, -149.0]])
    line = resample_line(path, step_miles=0.5, total_miles=34.6)
    east = -149.0 + 4 / (MILES_PER_DEGREE * np.cos(np.radians(60)))
    index = StationIndex.from_stations([station(1, 60.0, east, "3.00")])

    assert len(stations_along_route(line, index, corridor_miles=5.0).candidates) == 1


def test_no_stations_means_no_candidates(equator_route):
    match = stations_along_route(equator_route, StationIndex.from_stations([]), 5.0)

    assert match.candidates == []


@pytest.mark.django_db
def test_index_holds_geocoded_stations_only():
    common = dict(address="I-1", city="X", state="TX", country="US", price_samples=1)
    FuelStation.objects.create(
        opis_id=1,
        name="PLACED",
        price_per_gallon=Decimal("3.123"),
        latitude=31.0,
        longitude=-97.0,
        geocode_source="gazetteer",
        **common,
    )
    FuelStation.objects.create(
        opis_id=2, name="LOST", price_per_gallon=Decimal("3"), geocode_source="unresolved", **common
    )

    index = get_station_index()

    assert index.ids.tolist() == [1]
    assert index.price.tolist() == [3.123]
    assert index.info[1].price_per_gallon == Decimal("3.123")
