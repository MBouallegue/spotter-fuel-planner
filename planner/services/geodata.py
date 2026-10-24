"""Offline data preparation: clean the fuel-price CSV, merge duplicate stations and give
each station town-level coordinates from a gazetteer. Nothing here calls the network.

`resolve_town` is also used at request time, so "City, ST" input follows exactly the
same placement rules as the stations.
"""

import csv
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from itertools import combinations
from pathlib import Path
from typing import Literal, Protocol

from django.conf import settings

from planner.models import Country, Place
from planner.services.geometry import haversine_miles
from planner.services.normalize import CA_PROVINCES, normalize_town

DATA_DIR = settings.BASE_DIR / "data"
FUEL_PRICES_CSV = DATA_DIR / "raw" / "fuel-prices-for-be-assessment.csv"
GAZETTEER_CSV = DATA_DIR / "geo" / "us_cities.csv"
OVERRIDES_CSV = DATA_DIR / "geo" / "place_overrides.csv"
OVERRIDE_COLUMNS = ["city", "state", "country", "latitude", "longitude", "note"]

# Same-name gazetteer towns closer together than this are one place, so we average them.
SAME_PLACE_MILES = 25.0

TownKey = tuple[str, str]  # (normalize_town(city), state code)

_DIGITS = re.compile(r"[0-9]+")


def clean(value: str | None) -> str:
    """Strip and collapse inner whitespace: "  Port   Wentworth " -> "Port Wentworth"."""
    return " ".join((value or "").split())


def town_key(city: str, state: str) -> TownKey:
    return normalize_town(city), state.upper()


def country_for_state(state: str) -> str:
    return Country.CA if state in CA_PROVINCES else Country.US


# --- Fuel stations ------------------------------------------------------------------


@dataclass(frozen=True)
class FuelRow:
    """One cleaned, validated line of the fuel-price CSV."""

    opis_id: int
    name: str
    address: str
    city: str
    state: str
    rack_id: int | None
    price: Decimal


@dataclass(frozen=True)
class MergedStation:
    """Every row of one OPIS ID folded into a single station."""

    opis_id: int
    name: str
    address: str
    city: str
    state: str
    country: str
    rack_id: int | None
    price: Decimal  # mean of the rows, 3 dp, ROUND_HALF_UP
    price_samples: int
    price_spread: Decimal  # max - min of the rows; only reported


def read_fuel_rows(path: Path) -> tuple[list[FuelRow], list[str]]:
    """Return the valid rows, plus one message per skipped row."""
    rows: list[FuelRow] = []
    skipped: list[str] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for line_no, raw in enumerate(csv.DictReader(f), start=2):
            values = {key: clean(value) for key, value in raw.items() if key is not None}
            try:
                rows.append(_parse_fuel_row(values))
            except ValueError as exc:
                skipped.append(f"line {line_no}: {exc}")
    return rows, skipped


def _parse_fuel_row(values: dict[str, str]) -> FuelRow:
    opis_text = values.get("OPIS Truckstop ID", "")
    if not _DIGITS.fullmatch(opis_text):
        raise ValueError(f"OPIS ID {opis_text!r} is not an integer")

    price_text = values.get("Retail Price", "")
    try:
        price = Decimal(price_text)
    except InvalidOperation:
        raise ValueError(f"price {price_text!r} is not a number") from None
    if not price.is_finite() or price <= 0:
        raise ValueError(f"price {price_text!r} is not positive")

    state = values.get("State", "").upper()
    if len(state) != 2 or not state.isalpha():
        raise ValueError(f"state {state!r} is not a 2-letter code")

    rack_text = values.get("Rack ID", "")
    return FuelRow(
        opis_id=int(opis_text),
        name=values.get("Truckstop Name", ""),
        address=values.get("Address", ""),
        city=values.get("City", ""),
        state=state,
        rack_id=int(rack_text) if _DIGITS.fullmatch(rack_text) else None,
        price=price,
    )


def merge_by_opis_id(rows: Iterable[FuelRow]) -> tuple[list[MergedStation], list[int]]:
    """Fold the rows of each OPIS ID into one station.

    The price is the mean of the rows (597 IDs list different prices), and the name is the
    most common spelling ("PILOT TRAVEL CENTER #1243" vs "PILOT #1243"). Also returns the
    IDs whose address/city/state/rack disagree, which the data says never happens, so the
    caller can warn about them.
    """
    groups: dict[int, list[FuelRow]] = defaultdict(list)
    for row in rows:
        groups[row.opis_id].append(row)

    stations: list[MergedStation] = []
    inconsistent: list[int] = []
    for opis_id, group in groups.items():
        first = group[0]
        location = (first.address, first.city, first.state, first.rack_id)
        if any((r.address, r.city, r.state, r.rack_id) != location for r in group):
            inconsistent.append(opis_id)

        prices = [row.price for row in group]
        mean_price = (sum(prices) / len(prices)).quantize(Decimal("0.001"), ROUND_HALF_UP)
        names = Counter(row.name for row in group)
        stations.append(
            MergedStation(
                opis_id=opis_id,
                # max() keeps the first of equal counts, and Counter keeps first-seen order.
                name=max(names, key=names.__getitem__),
                address=first.address,
                city=first.city,
                state=first.state,
                country=country_for_state(first.state),
                rack_id=first.rack_id,
                price=mean_price,
                price_samples=len(group),
                price_spread=max(prices) - min(prices),
            )
        )
    return stations, inconsistent


# --- Places (offline geocoding) -----------------------------------------------------


@dataclass(frozen=True)
class PlaceRow:
    """A town centre read from a CSV, with the same fields as the Place model."""

    name: str
    name_key: str
    state: str
    country: str
    latitude: float
    longitude: float
    source: str


class PlaceLike(Protocol):
    """Anything with a Place's location fields: a PlaceRow, a Place, or a named values_list row."""

    name_key: str
    state: str
    latitude: float
    longitude: float
    source: str


ResolveRule = Literal["override", "single", "averaged", "far_apart"]


@dataclass(frozen=True)
class Resolution:
    """Where a town key landed, and which rule decided it."""

    latitude: float
    longitude: float
    source: str  # Place.Source value
    rule: ResolveRule
    gazetteer_rows: int  # gazetteer rows sharing the key, for the import report


def read_gazetteer(path: Path) -> list[PlaceRow]:
    """kelvins/US-Cities-Database: ID, STATE_CODE, STATE_NAME, CITY, COUNTY, LATITUDE, LONGITUDE."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [
            PlaceRow(
                name=clean(row["CITY"]),
                name_key=normalize_town(row["CITY"]),
                state=clean(row["STATE_CODE"]).upper(),
                country=Country.US,
                latitude=float(row["LATITUDE"]),
                longitude=float(row["LONGITUDE"]),
                source=Place.Source.GAZETTEER,
            )
            for row in csv.DictReader(f)
        ]


def read_overrides(path: Path) -> list[PlaceRow]:
    """Hand-checked towns: city (spelled as in the fuel CSV), state, country, lat, lon, note."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [
            PlaceRow(
                name=clean(row["city"]),
                name_key=normalize_town(row["city"]),
                state=clean(row["state"]).upper(),
                country=clean(row["country"]).upper(),
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
                source=Place.Source.OVERRIDE,
            )
            for row in csv.DictReader(f)
        ]


def index_places(places: Iterable[PlaceLike]) -> dict[TownKey, list[PlaceLike]]:
    """Group places by town key, keeping input order (which decides "first" below)."""
    index: dict[TownKey, list[PlaceLike]] = defaultdict(list)
    for place in places:
        index[(place.name_key, place.state)].append(place)
    return dict(index)


def resolve_town(places: Sequence[PlaceLike]) -> Resolution | None:
    """Pick one coordinate for the places that share a town key, or None if there are none.

    1. An override wins: it exists because the gazetteer lacks the town or has it wrong.
    2. A single gazetteer row is used as it is.
    3. Several gazetteer rows (same name, different counties) are averaged when all lie
       within 25 miles of each other. Otherwise the name is ambiguous: the first row is
       used and the caller should warn, so someone can add an override.
    """
    gazetteer = [p for p in places if p.source == Place.Source.GAZETTEER]
    override = next((p for p in places if p.source == Place.Source.OVERRIDE), None)
    if override is not None:
        return Resolution(
            override.latitude, override.longitude, Place.Source.OVERRIDE, "override", len(gazetteer)
        )
    if not gazetteer:
        return None
    if len(gazetteer) == 1:
        only = gazetteer[0]
        return Resolution(only.latitude, only.longitude, Place.Source.GAZETTEER, "single", 1)
    if _max_spread_miles(gazetteer) <= SAME_PLACE_MILES:
        latitude = sum(p.latitude for p in gazetteer) / len(gazetteer)
        longitude = sum(p.longitude for p in gazetteer) / len(gazetteer)
        return Resolution(latitude, longitude, Place.Source.GAZETTEER, "averaged", len(gazetteer))
    first = gazetteer[0]
    return Resolution(
        first.latitude, first.longitude, Place.Source.GAZETTEER, "far_apart", len(gazetteer)
    )


def _max_spread_miles(places: Sequence[PlaceLike]) -> float:
    return max(
        float(haversine_miles(a.latitude, a.longitude, b.latitude, b.longitude))
        for a, b in combinations(places, 2)
    )
