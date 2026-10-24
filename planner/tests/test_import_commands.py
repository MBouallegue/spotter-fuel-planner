import shutil
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from planner.models import FuelStation, Place

pytestmark = pytest.mark.django_db


@pytest.fixture
def overrides_csv(tmp_path, fixtures_dir):
    """A writable copy of the sample overrides, so a test can append to it."""
    path = tmp_path / "place_overrides.csv"
    shutil.copy(fixtures_dir / "place_overrides_sample.csv", path)
    return path


def run_imports(fixtures_dir, overrides_csv) -> tuple[str, str]:
    call_command(
        "import_places",
        gazetteer=fixtures_dir / "us_cities_sample.csv",
        overrides=overrides_csv,
        stdout=StringIO(),
    )
    out, err = StringIO(), StringIO()
    call_command(
        "import_stations", csv_path=fixtures_dir / "fuel_prices_sample.csv", stdout=out, stderr=err
    )
    return out.getvalue(), err.getvalue()


def test_import_places_loads_gazetteer_and_overrides_and_can_rerun(fixtures_dir, overrides_csv):
    for _ in range(2):
        call_command(
            "import_places",
            gazetteer=fixtures_dir / "us_cities_sample.csv",
            overrides=overrides_csv,
            stdout=StringIO(),
        )

    assert Place.objects.filter(source="gazetteer").count() == 7
    assert Place.objects.filter(source="override").count() == 2


def test_import_stations_requires_places_first(fixtures_dir):
    with pytest.raises(CommandError, match="import_places"):
        call_command("import_stations", csv_path=fixtures_dir / "fuel_prices_sample.csv")


def test_import_stations_merges_cleans_and_geocodes(fixtures_dir, overrides_csv):
    with overrides_csv.open("a", encoding="utf-8") as f:
        f.write("Nowhere,ME,US,45.0,-69.0,test: fills the last gap\n")

    report, warnings = run_imports(fixtures_dir, overrides_csv)
    stations = {station.opis_id: station for station in FuelStation.objects.all()}

    assert sorted(stations) == [101, 102, 103, 104, 105, 106]
    tomah = stations[101]
    assert tomah.price_per_gallon == Decimal("3.003")  # mean 3.0025, rounded half-up
    assert (tomah.price_samples, tomah.name) == (3, "PILOT TRAVEL CENTER #1")
    assert (tomah.city, tomah.address) == ("Tomah", "I-94, EXIT 143")  # stray spaces gone

    twin = stations[102]
    assert (twin.name, twin.rack_id, twin.geocode_source) == ("TIE NAME A", None, "gazetteer")
    assert twin.latitude == pytest.approx(31.05)  # two rows 7 mi apart, averaged

    assert stations[103].latitude == 30.0  # two rows 138 mi apart: the first one, with a warning
    assert "FAR JUNCTION, TX has same-name gazetteer towns" in warnings

    assert (stations[104].country, stations[104].geocode_source) == ("CA", "override")
    orlando = stations[105]
    assert (orlando.latitude, orlando.longitude) == (28.5383, -81.3792)  # override wins

    assert "Rows read ...................... 12      (skipped: 3)" in report
    assert "Unique stations ................ 6       (US 5, CA 1)" in report
    assert "Unresolved ..................... 0" in report


def test_import_stations_fails_loudly_when_too_many_towns_are_unresolved(
    fixtures_dir, overrides_csv
):
    with pytest.raises(CommandError, match=r"1 stations \(16.7%\) have no coordinates"):
        run_imports(fixtures_dir, overrides_csv)

    # The import still lands, with the unplaceable station marked as such.
    lost = FuelStation.objects.get(opis_id=106)
    assert (lost.geocode_source, lost.latitude, lost.longitude) == ("unresolved", None, None)
    assert FuelStation.objects.count() == 6
