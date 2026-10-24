"""Every geocoded station held in memory as numpy arrays, built once per process.

Matching a route is then pure array maths, with no database query per station.
The index is not refreshed: restart the server after re-importing stations.
"""

import functools
from collections.abc import Iterable
from dataclasses import dataclass, fields
from decimal import Decimal

import numpy as np

from planner.models import FuelStation
from planner.services.geometry import unit_vectors


@dataclass(frozen=True)
class StationInfo:
    """What the response shows about a station. Field names match FuelStation."""

    opis_id: int
    name: str
    address: str
    city: str
    state: str
    country: str
    latitude: float
    longitude: float
    price_per_gallon: Decimal  # exact, for the cost accounting


@dataclass(frozen=True)
class StationIndex:
    """Parallel arrays, one entry per station, plus display fields by OPIS ID."""

    ids: np.ndarray  # OPIS IDs
    lat: np.ndarray
    lon: np.ndarray
    price: np.ndarray  # USD per gallon as floats, for the optimizer
    xyz: np.ndarray  # unit-sphere points, shape (n, 3), for the KD-tree query
    info: dict[int, StationInfo]

    @classmethod
    def from_stations(cls, stations: Iterable[StationInfo]) -> "StationIndex":
        rows = list(stations)
        lat = np.array([s.latitude for s in rows], dtype=float)
        lon = np.array([s.longitude for s in rows], dtype=float)
        return cls(
            ids=np.array([s.opis_id for s in rows], dtype=np.int64),
            lat=lat,
            lon=lon,
            price=np.array([s.price_per_gallon for s in rows], dtype=float),
            xyz=unit_vectors(lat, lon),
            info={s.opis_id: s for s in rows},
        )


@functools.cache
def get_station_index() -> StationIndex:
    """Load the stations on first use, then reuse them for the life of the process.

    Never called at import time or from AppConfig.ready(): the database may not exist yet.
    """
    rows = (
        FuelStation.objects.exclude(geocode_source=FuelStation.GeocodeSource.UNRESOLVED)
        .order_by("opis_id")
        .values(*(field.name for field in fields(StationInfo)))
    )
    return StationIndex.from_stations(StationInfo(**row) for row in rows)
