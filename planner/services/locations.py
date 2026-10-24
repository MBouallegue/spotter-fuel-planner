"""Turn a start or finish input into a coordinate inside the USA.

Tried in order, cheapest first:
1. Coordinates, as {"lat": .., "lon": ..} or "lat,lon" text: nothing to look up.
2. "City, ST" or "City, State": the offline gazetteer, with the same placement rules as
   the stations. No network call.
3. Anything else (a street address, a ZIP code, a landmark): one Nominatim search,
   cached for 30 days.
"""

import hashlib
import logging
import re
import time
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

from planner.models import Country, Place
from planner.services.geodata import resolve_town
from planner.services.normalize import US_STATES, normalize_town

logger = logging.getLogger(__name__)

LocationInput = str | dict[str, float]  # text, or {"lat": .., "lon": ..} from the serializer

GEOCODE_CACHE_SECONDS = 30 * 24 * 60 * 60
_LAT_LON = re.compile(r"\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*")
# Both "TX" and "Texas", in any case, map to "TX".
_STATE_CODES = {code: code for code in US_STATES} | {
    name.upper(): code for code, name in US_STATES.items()
}

# Rough boxes around the lower 48, Alaska and Hawaii: (min_lat, max_lat, min_lon, max_lon).
# They turn away Paris or Mexico City, but let through points just across a border.
US_BOXES = [
    (24.4, 49.5, -125.0, -66.9),
    (51.0, 72.0, -180.0, -129.9),
    (18.9, 22.3, -160.3, -154.8),
]


@dataclass(frozen=True)
class ResolvedLocation:
    query: LocationInput  # what the client sent
    label: str
    lat: float
    lon: float
    resolved_by: str  # "coordinates", "gazetteer", "override" or "nominatim"
    external_calls: int = 0  # Nominatim requests sent (0 when the answer was cached)

    @property
    def point(self) -> tuple[float, float]:
        return self.lat, self.lon


class LocationNotFound(Exception):
    pass


class LocationOutsideUSA(Exception):
    pass


class GeocodingUnavailable(Exception):
    """Nominatim did not answer, so a free-text location cannot be resolved right now."""


def resolve_location(value: LocationInput) -> ResolvedLocation:
    """Resolve one start/finish input, or raise LocationNotFound, LocationOutsideUSA or
    GeocodingUnavailable."""
    if isinstance(value, dict):
        location = _from_coordinates(value, value["lat"], value["lon"])
    elif match := _LAT_LON.fullmatch(value):
        location = _from_coordinates(value, float(match[1]), float(match[2]))
    else:
        location = _from_gazetteer(value) or _from_nominatim(value)

    if not in_usa(location.lat, location.lon):
        raise LocationOutsideUSA(
            f"{location.label} is outside the USA; start and finish must both be in the USA."
        )
    return location


def in_usa(lat: float, lon: float) -> bool:
    return any(
        lat_min <= lat <= lat_max and lon_min <= lon <= lon_max
        for lat_min, lat_max, lon_min, lon_max in US_BOXES
    )


def _from_coordinates(query: LocationInput, lat: float, lon: float) -> ResolvedLocation:
    return ResolvedLocation(query, f"{lat:.5f}, {lon:.5f}", lat, lon, "coordinates")


def _from_gazetteer(text: str) -> ResolvedLocation | None:
    """Look up "City, ST" or "City, State" offline; None if the text is not in that form or
    the town is unknown."""
    city, comma, region = text.rpartition(",")
    state = _STATE_CODES.get(" ".join(region.split()).upper())
    if not comma or not state or not city.strip():
        return None

    places = list(
        Place.objects.filter(
            name_key=normalize_town(city), state=state, country=Country.US
        ).order_by("id")
    )
    where = resolve_town(places)
    if where is None:
        return None
    label = f"{places[0].name}, {state}"
    return ResolvedLocation(text, label, where.latitude, where.longitude, where.source)


def _from_nominatim(text: str) -> ResolvedLocation:
    normalized = " ".join(text.lower().split())
    key = "geo:" + hashlib.sha1(normalized.encode(), usedforsecurity=False).hexdigest()
    hit = cache.get(key)
    calls = 0
    if hit is None:
        hit = _nominatim_search(text)
        # Misses are cached too, so repeating a bad input costs no further calls.
        cache.set(key, hit, GEOCODE_CACHE_SECONDS)
        calls = 1
    if not hit:
        raise LocationNotFound(
            f'Could not find "{text}". Try "City, ST", a full street address or "lat,lon".'
        )
    return ResolvedLocation(text, hit["label"], hit["lat"], hit["lon"], "nominatim", calls)


def _nominatim_search(text: str) -> dict:
    """One free-text search limited to the USA: {"lat", "lon", "label"}, or {} if none."""
    params = {"q": text, "countrycodes": "us", "format": "jsonv2", "limit": "1"}
    started = time.perf_counter()
    try:
        response = requests.get(
            f"{settings.NOMINATIM_BASE_URL}/search",
            params=params,
            headers={"User-Agent": settings.HTTP_USER_AGENT},
            timeout=settings.NOMINATIM_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        results = response.json()
        hit = {}
        if results:
            best = results[0]
            hit = {
                "lat": float(best["lat"]),
                "lon": float(best["lon"]),
                "label": best["display_name"],
            }
    except (requests.RequestException, ValueError, LookupError, TypeError) as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.warning("nominatim call failed after %d ms: %r", elapsed_ms, exc)
        raise GeocodingUnavailable(
            'The address geocoder is not answering. Try "City, ST" or "lat,lon" instead.'
        ) from exc
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info("nominatim responded HTTP %s in %d ms", response.status_code, elapsed_ms)
    return hit
