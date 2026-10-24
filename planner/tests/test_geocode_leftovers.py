import json
import shutil
from io import StringIO

import pytest
import responses
from django.core.management import call_command
from responses import matchers

from planner.management.commands.geocode_leftovers import search_params

NOMINATIM = "https://nominatim.example/search"


@pytest.fixture
def nominatim(settings, monkeypatch):
    settings.NOMINATIM_BASE_URL = "https://nominatim.example"
    monkeypatch.setattr(
        "planner.management.commands.geocode_leftovers.time.sleep", lambda seconds: None
    )
    with responses.RequestsMock() as mock:
        yield mock


def test_search_params_use_aliases_and_full_region_names():
    assert search_params("Brookpark", "OH", "US")["city"] == "Brook Park"
    params = search_params("Kingston", "ON", "CA")
    assert (params["city"], params["state"], params["country"]) == ("Kingston", "Ontario", "Canada")


def test_looks_up_each_missing_town_once_and_appends_it(
    nominatim, settings, tmp_path, fixtures_dir
):
    overrides = tmp_path / "place_overrides.csv"
    shutil.copy(fixtures_dir / "place_overrides_sample.csv", overrides)
    cache = tmp_path / "nominatim_cache.json"
    nominatim.get(
        NOMINATIM,
        match=[
            matchers.query_param_matcher(
                {
                    "city": "Nowhere",
                    "state": "Maine",
                    "country": "United States",
                    "format": "jsonv2",
                    "limit": "1",
                }
            ),
            matchers.header_matcher({"User-Agent": settings.HTTP_USER_AGENT}),
        ],
        json=[{"lat": "45.25", "lon": "-69.5", "display_name": "Nowhere, Maine, United States"}],
    )
    options = dict(
        csv_path=fixtures_dir / "fuel_prices_sample.csv",
        gazetteer=fixtures_dir / "us_cities_sample.csv",
        overrides=overrides,
        cache=cache,
        stdout=StringIO(),
        stderr=StringIO(),
    )

    call_command("geocode_leftovers", **options)
    # The town is in the overrides file now, so a second run has nothing to look up.
    call_command("geocode_leftovers", **options)

    assert len(nominatim.calls) == 1
    assert overrides.read_text().splitlines()[-1] == (
        'Nowhere,ME,US,45.250000,-69.500000,"nominatim: Nowhere, Maine, United States"'
    )
    assert len(json.loads(cache.read_text())) == 1
