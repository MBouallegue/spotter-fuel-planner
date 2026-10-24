from pathlib import Path

import pytest
from django.core.cache import cache


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def fresh_process_caches():
    """Start every test like a new process: empty Django cache, no station index, and no
    memoized route geometry."""
    from planner.services.station_index import get_station_index
    from planner.services.trip_planner import route_geometry

    cache.clear()
    get_station_index.cache_clear()
    route_geometry.cache_clear()
