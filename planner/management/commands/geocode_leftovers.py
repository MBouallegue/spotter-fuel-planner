"""One-time helper: find the station towns the offline data cannot place, look each one up on
Nominatim, and append the answers to place_overrides.csv for a human to review.

It follows Nominatim's policy for small one-off jobs: a single thread, at most one request
per second, an identifying User-Agent, and every answer cached. Reviewers never need to
run it, because its output is committed.
"""

import csv
import json
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from planner.models import Country
from planner.services.geodata import (
    DATA_DIR,
    FUEL_PRICES_CSV,
    GAZETTEER_CSV,
    OVERRIDE_COLUMNS,
    OVERRIDES_CSV,
    TownKey,
    index_places,
    merge_by_opis_id,
    read_fuel_rows,
    read_gazetteer,
    read_overrides,
    resolve_town,
    town_key,
)
from planner.services.normalize import CA_PROVINCES, US_STATES

CACHE_JSON = DATA_DIR / "geo" / ".nominatim_cache.json"
MIN_INTERVAL_SECONDS = 1.1

# Towns the CSV names differently from OpenStreetMap. The exits listed in the CSV are the evidence.
SEARCH_ALIASES = {
    ("BROOKPARK", "OH"): "Brook Park",  # I-480 exit 12
    ("ELIZABETHPORT", "NJ"): "Elizabeth",  # I-95 exit 13A; Elizabethport is part of Elizabeth
    ("HENRICO", "VA"): "Sandston",  # I-64 exit 197A and I-295 exit 22A: eastern Henrico County
}


class Command(BaseCommand):
    help = "Look up unplaceable station towns on Nominatim and append them to place_overrides.csv."

    def add_arguments(self, parser):
        parser.add_argument("--csv", dest="csv_path", type=Path, default=FUEL_PRICES_CSV)
        parser.add_argument("--gazetteer", type=Path, default=GAZETTEER_CSV)
        parser.add_argument("--overrides", type=Path, default=OVERRIDES_CSV)
        parser.add_argument("--cache", type=Path, default=CACHE_JSON)

    def handle(
        self, *args, csv_path: Path, gazetteer: Path, overrides: Path, cache: Path, **options
    ):
        towns = leftover_towns(csv_path, gazetteer, overrides)
        if not towns:
            self.stdout.write("Every station town already has coordinates. Nothing to look up.")
            return

        self.stdout.write(f"Looking up {len(towns)} towns on Nominatim (about 1 per second)...")
        client = NominatimClient(cache)
        found: list[list[str]] = []
        missing: list[str] = []
        for city, state, country in towns:
            hit = client.search(search_params(city, state, country))
            if hit is None:
                missing.append(f"{city}, {state}")
                continue
            found.append(
                [
                    city,
                    state,
                    country,
                    f"{float(hit['lat']):.6f}",
                    f"{float(hit['lon']):.6f}",
                    f"nominatim: {hit['display_name']}",
                ]
            )

        append_overrides(overrides, found)
        for row in found:
            self.stdout.write("  " + " | ".join(row))
        self.stdout.write(
            self.style.SUCCESS(
                f"Appended {len(found)} rows to {overrides}. Review them, then re-run "
                "import_places and import_stations."
            )
        )
        if missing:
            self.stderr.write("No Nominatim result for: " + "; ".join(missing))


def leftover_towns(csv_path: Path, gazetteer: Path, overrides: Path) -> list[tuple[str, str, str]]:
    """Station towns with neither an override nor a gazetteer entry, as (city, state, country)."""
    rows, _ = read_fuel_rows(csv_path)
    stations, _ = merge_by_opis_id(rows)
    places = index_places(read_gazetteer(gazetteer) + read_overrides(overrides))

    leftovers: dict[TownKey, tuple[str, str, str]] = {}
    for station in stations:
        key = town_key(station.city, station.state)
        if key not in leftovers and resolve_town(places.get(key, [])) is None:
            leftovers[key] = (station.city, station.state, station.country)
    return sorted(leftovers.values(), key=lambda town: (town[2] != Country.US, town[1], town[0]))


def search_params(city: str, state: str, country: str) -> dict[str, str]:
    """Structured search: the city (or its alias), full state or province name, country."""
    if country == Country.CA:
        region, country_name = CA_PROVINCES[state], "Canada"
    else:
        region, country_name = US_STATES[state], "United States"
    return {
        "city": SEARCH_ALIASES.get(town_key(city, state), city),
        "state": region,
        "country": country_name,
        "format": "jsonv2",
        "limit": "1",
    }


class NominatimClient:
    """Sequential, throttled and cached, so a re-run sends no requests for known towns."""

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self.cache: dict[str, list[dict]] = (
            json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        )
        self.session = requests.Session()
        self.session.headers["User-Agent"] = settings.HTTP_USER_AGENT
        self.last_request_at: float | None = None

    def search(self, params: dict[str, str]) -> dict | None:
        key = urlencode(sorted(params.items()))
        if key not in self.cache:
            self._wait_turn()
            try:
                response = self.session.get(
                    f"{settings.NOMINATIM_BASE_URL}/search",
                    params=params,
                    timeout=settings.NOMINATIM_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                self.cache[key] = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise CommandError(f"Nominatim request failed for {key}: {exc}") from exc
            # Saved after every answer, so an interrupted run resumes where it stopped.
            self.cache_path.write_text(json.dumps(self.cache, indent=1), encoding="utf-8")
        results = self.cache[key]
        return results[0] if results else None

    def _wait_turn(self) -> None:
        if self.last_request_at is not None:
            wait = MIN_INTERVAL_SECONDS - (time.monotonic() - self.last_request_at)
            if wait > 0:
                time.sleep(wait)
        self.last_request_at = time.monotonic()


def append_overrides(path: Path, rows: list[list[str]]) -> None:
    is_new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        if is_new:
            writer.writerow(OVERRIDE_COLUMNS)
        writer.writerows(rows)
