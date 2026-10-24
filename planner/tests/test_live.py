"""Smoke tests against the real services. Skipped by default; run with `pytest -m live`."""

import polyline
import pytest

from planner.models import METERS_PER_MILE
from planner.services.routing.osrm import OsrmProvider

pytestmark = pytest.mark.live


def test_osrm_routes_dallas_to_houston(settings):
    provider = OsrmProvider(settings.OSRM_BASE_URL, settings.ROUTING_TIMEOUT_SECONDS)

    result = provider.route((32.7767, -96.7970), (29.7604, -95.3698))

    assert 220 < result.distance_m / METERS_PER_MILE < 260
    assert len(polyline.decode(result.polyline6, 6)) > 100
