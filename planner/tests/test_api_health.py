from decimal import Decimal

import pytest

from planner.models import FuelStation
from planner.services.station_index import get_station_index


@pytest.mark.django_db
def test_health_reports_stations_and_provider_states(client, settings):
    settings.ROUTING_PROVIDERS = ["osrm", "ors"]
    settings.ORS_API_KEY = ""

    response = client.get("/api/v1/health/")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "stations": 0,
        "providers": {"osrm": "closed", "ors": "disabled"},
    }


@pytest.mark.django_db
def test_health_warms_the_station_index(client):
    FuelStation.objects.create(
        opis_id=1,
        name="STOP",
        address="I-40",
        city="Amarillo",
        state="TX",
        country="US",
        price_per_gallon=Decimal("3.100"),
        latitude=35.2,
        longitude=-101.8,
        geocode_source="gazetteer",
    )

    assert client.get("/api/v1/health/").json()["stations"] == 1
    assert get_station_index.cache_info().currsize == 1  # built, so a first plan skips it
