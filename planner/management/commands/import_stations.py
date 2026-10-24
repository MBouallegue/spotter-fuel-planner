"""Import fuel stations: clean the CSV, merge duplicate OPIS IDs, geocode offline, report."""

from collections import Counter
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from planner.models import Country, FuelStation, Place
from planner.services.geodata import (
    FUEL_PRICES_CSV,
    MergedStation,
    Resolution,
    TownKey,
    index_places,
    merge_by_opis_id,
    read_fuel_rows,
    resolve_town,
    town_key,
)

# Above this share of unplaceable stations the place data is broken, so fail loudly.
MAX_UNRESOLVED_SHARE = 0.005


class Command(BaseCommand):
    help = "Import fuel stations from the OPIS CSV: clean, merge duplicate IDs, geocode offline."

    def add_arguments(self, parser):
        parser.add_argument("--csv", dest="csv_path", type=Path, default=FUEL_PRICES_CSV)

    def handle(self, *args, csv_path: Path, **options):
        if not Place.objects.exists():
            raise CommandError("No places loaded yet. Run `manage.py import_places` first.")

        rows, skipped = read_fuel_rows(csv_path)
        stations, inconsistent = merge_by_opis_id(rows)
        # Named tuples instead of model instances: 30k ORM objects are slow to build.
        places = index_places(
            Place.objects.order_by("id").values_list(
                "name_key", "state", "latitude", "longitude", "source", named=True
            )
        )
        resolutions = {
            s.opis_id: resolve_town(places.get(town_key(s.city, s.state), [])) for s in stations
        }

        with transaction.atomic():
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(
                [_to_model(s, resolutions[s.opis_id]) for s in stations], batch_size=1000
            )

        self._report(len(rows) + len(skipped), skipped, stations, inconsistent, resolutions)

        unresolved = sum(1 for r in resolutions.values() if r is None)
        if unresolved > MAX_UNRESOLVED_SHARE * len(stations):
            raise CommandError(
                f"{unresolved} stations ({unresolved / len(stations):.1%}) have no coordinates, "
                f"above the {MAX_UNRESOLVED_SHARE:.1%} limit. Add their towns to "
                "data/geo/place_overrides.csv (see `manage.py geocode_leftovers`)."
            )

    def _report(
        self,
        rows_read: int,
        skipped: list[str],
        stations: list[MergedStation],
        inconsistent: list[int],
        resolutions: dict[int, Resolution | None],
    ) -> None:
        us_stations = sum(1 for s in stations if s.country == Country.US)
        towns: dict[TownKey, str] = {town_key(s.city, s.state): s.country for s in stations}
        us_towns = sum(1 for country in towns.values() if country == Country.US)

        merged = [s for s in stations if s.price_samples > 1]
        differing = [s for s in merged if s.price_spread > 0]
        max_spread = max((s.price_spread for s in merged), default=Decimal(0))

        sources = Counter(r.source if r else "unresolved" for r in resolutions.values())
        override_kinds = Counter()
        for s in stations:
            where = resolutions[s.opis_id]
            if where and where.source == Place.Source.OVERRIDE:
                override_kinds["fix" if where.gazetteer_rows else s.country] += 1

        # Town keys with several gazetteer rows, and how each was settled.
        repeated: dict[TownKey, Resolution] = {}
        for s in stations:
            where = resolutions[s.opis_id]
            if where and where.gazetteer_rows > 1:
                repeated[town_key(s.city, s.state)] = where
        rules = Counter(where.rule for where in repeated.values())
        repeated_detail = f"{rules['averaged']} averaged <=25 mi, {rules['override']} overridden"
        if rules["far_apart"]:
            repeated_detail += f", {rules['far_apart']} far apart (first row used)"

        lines = [
            _line("Rows read", rows_read, f"(skipped: {len(skipped)})"),
            _line(
                "Unique stations",
                len(stations),
                f"(US {us_stations}, CA {len(stations) - us_stations})",
            ),
            _line("Station towns", len(towns), f"(US {us_towns}, CA {len(towns) - us_towns})"),
            _line(
                "IDs merged (duplicates)",
                len(merged),
                f"({len(differing)} with differing prices, max spread ${max_spread:.2f})",
            ),
            _line("Geocoded: gazetteer", sources[Place.Source.GAZETTEER]),
            _line(
                "Geocoded: overrides",
                sources[Place.Source.OVERRIDE],
                f"(US {override_kinds[Country.US]}, CA {override_kinds[Country.CA]}, "
                f"replacing a gazetteer entry {override_kinds['fix']})",
            ),
            _line("Unresolved", sources["unresolved"]),
            _line("Repeated gazetteer keys hit", len(repeated), f"({repeated_detail})"),
        ]
        self.stdout.write("\n".join(lines))

        for message in skipped:
            self.stderr.write(f"  skipped {message}")
        for opis_id in inconsistent:
            self.stderr.write(f"  warning: OPIS {opis_id} rows disagree on address/city/state/rack")
        for key, where in repeated.items():
            if where.rule == "far_apart":
                self.stderr.write(
                    f"  warning: {key[0]}, {key[1]} has same-name gazetteer towns more than "
                    "25 mi apart; used the first. Add an override to choose one."
                )
        for s in stations:
            if resolutions[s.opis_id] is None:
                self.stderr.write(f"  unresolved: OPIS {s.opis_id} {s.name} - {s.city}, {s.state}")


def _line(label: str, value: int, detail: str = "") -> str:
    """One report row: "Rows read ...................... 8151    (skipped: 0)"."""
    return f"{label} {'.' * (31 - len(label))} {value:<7} {detail}".rstrip()


def _to_model(station: MergedStation, where: Resolution | None) -> FuelStation:
    return FuelStation(
        opis_id=station.opis_id,
        name=station.name,
        address=station.address,
        city=station.city,
        state=station.state,
        country=station.country,
        rack_id=station.rack_id,
        price_per_gallon=station.price,
        price_samples=station.price_samples,
        latitude=where.latitude if where else None,
        longitude=where.longitude if where else None,
        geocode_source=where.source if where else FuelStation.GeocodeSource.UNRESOLVED,
    )
