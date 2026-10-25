"""Plan a trip end to end, shape the response body and save it.

resolve start and finish -> route (cached) -> resample the line every 0.5 mi -> stations
within the corridor -> cheapest purchases -> stops, totals, a GeoJSON map and timings ->
saved as a TripPlan, which the map page reads.
"""

import functools
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urljoin

import numpy as np
import polyline
from django.urls import reverse

from planner.models import METERS_PER_MILE, RouteCache, TripPlan
from planner.services.geometry import RouteLine, haversine_miles, resample_line, simplify_line
from planner.services.locations import LocationInput, ResolvedLocation, resolve_location
from planner.services.matching import CorridorMatch, stations_along_route
from planner.services.optimizer import NoFeasiblePlan, Purchase, plan_fuel_stops
from planner.services.routing.service import RoutingService
from planner.services.station_index import StationInfo, get_station_index

# About 2 points per mile: mile markers land within a quarter mile, far finer than the
# town-level station coordinates, and the KD-tree stays small.
ROUTE_STEP_MILES = 0.5
MIN_TRIP_MILES = 1.0
MAX_MAP_POINTS = 2000

THOUSANDTHS = Decimal("0.001")  # gallons and prices are shown to 3 dp
CENTS = Decimal("0.01")
FUEL_COST_NOTE = "total_fuel_cost is the fuel bought during the trip."


class SameStartAndFinish(Exception):
    pass


class TripInfeasible(Exception):
    """A gap between fuel points is longer than the vehicle can drive."""

    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


@dataclass(frozen=True)
class FuelStop:
    """A purchase, its station, and the amounts rounded exactly as the response shows them."""

    purchase: Purchase
    station: StationInfo
    gallons: Decimal  # 3 dp
    cost: Decimal  # the shown gallons times the price, rounded half-up to the cent

    @classmethod
    def priced(cls, purchase: Purchase, station: StationInfo) -> "FuelStop":
        # Costs come from the gallons as shown, so a client can check every stop's
        # arithmetic, and the total is exactly the sum of the stops.
        gallons = Decimal(purchase.gallons).quantize(THOUSANDTHS, ROUND_HALF_UP)
        cost = (gallons * station.price_per_gallon).quantize(CENTS, ROUND_HALF_UP)
        return cls(purchase, station, gallons, cost)


@dataclass(frozen=True)
class RouteGeometry:
    line: RouteLine  # resampled every 0.5 mi, with mile markers, for matching
    map_coordinates: tuple[tuple[float, float], ...]  # simplified GeoJSON [lon, lat] positions


@functools.lru_cache(maxsize=32)
def route_geometry(polyline6: str, total_miles: float) -> RouteGeometry:
    """Decode, resample and simplify a route once per process, then reuse the result.

    Decoding a cross-country polyline (about 35,000 points) takes about 100 ms in pure
    Python, several times the rest of a warm request. The result is a pure function of
    the arguments, so the cache can never go stale.
    """
    path = np.array(polyline.decode(polyline6, 6))  # (n, 2) array of (lat, lon)
    simplified = simplify_line(path, MAX_MAP_POINTS)
    return RouteGeometry(
        line=resample_line(path, ROUTE_STEP_MILES, total_miles),
        map_coordinates=tuple(_position(lat, lon) for lat, lon in simplified.tolist()),
    )


def plan_trip(
    start: LocationInput,
    finish: LocationInput,
    *,
    base_url: str,
    max_range_miles: float = 500.0,
    mpg: float = 10.0,
    start_fuel_gallons: float | None = None,
    corridor_miles: float = 5.0,
) -> dict:
    """Plan the cheapest fuel stops between two US locations and return the response body.

    The body is saved as a TripPlan, so its map page and GET /trips/<id>/ can show it later
    without planning again; ``base_url`` (the request's scheme and host) makes map_url
    absolute. ``start_fuel_gallons=None`` means the tank starts full. Raises the domain
    errors that api/errors.py turns into HTTP responses.
    """
    started = time.perf_counter()
    origin = resolve_location(start)
    destination = resolve_location(finish)
    if haversine_miles(*origin.point, *destination.point) < MIN_TRIP_MILES:
        raise SameStartAndFinish("Start and finish are less than 1 mile apart.")
    geocoded = time.perf_counter()

    lookup = RoutingService().get_route(origin.point, destination.point)
    routed = time.perf_counter()

    route = lookup.route
    total_miles = route.distance_m / METERS_PER_MILE
    geometry = route_geometry(route.polyline6, total_miles)
    index = get_station_index()
    matches = stations_along_route(geometry.line, index, corridor_miles)
    try:
        purchases = plan_fuel_stops(
            matches.candidates, total_miles, max_range_miles, mpg, start_fuel_gallons
        )
    except NoFeasiblePlan as exc:
        raise _infeasible(exc, geometry.line) from exc

    tank = max_range_miles / mpg
    start_fuel = tank if start_fuel_gallons is None else start_fuel_gallons
    stops = [FuelStop.priced(p, index.info[p.candidate.station_id]) for p in purchases]
    route_json = _route_json(route)
    fuel_stops = _fuel_stops_json(stops, matches)
    plan = TripPlan(
        route=route,
        start_query=_query_text(start),
        finish_query=_query_text(finish),
        params={
            "max_range_miles": max_range_miles,
            "mpg": mpg,
            "start_fuel_gallons": start_fuel_gallons,
            "corridor_miles": corridor_miles,
        },
    )
    body = {
        "id": str(plan.id),
        "map_url": urljoin(base_url, reverse("api:trip-map", args=[plan.id])),
        "start": _location_json(origin),
        "finish": _location_json(destination),
        "route": route_json,
        "vehicle": {
            "max_range_miles": max_range_miles,
            "mpg": mpg,
            "tank_gallons": round(tank, 3),
            "start_fuel_gallons": round(start_fuel, 3),
        },
        "fuel_stops": fuel_stops,
        "summary": _summary_json(stops, total_miles, mpg, start_fuel, tank),
        "map": _map_json(geometry.map_coordinates, origin, destination, route_json, fuel_stops),
    }
    planned = time.perf_counter()

    body["meta"] = {
        "external_calls": {
            "routing": lookup.external_calls,
            "geocoding": origin.external_calls + destination.external_calls,
        },
        "route_cache": "hit" if lookup.cache_hit else "miss",
        "candidate_stations": len(matches.candidates),
        "timing_ms": {
            "total": _ms(started, planned),
            "geocoding": _ms(started, geocoded),
            "routing": _ms(geocoded, routed),
            "planning": _ms(routed, planned),
        },
    }
    plan.result = body
    plan.save()
    return body


def _query_text(value: LocationInput) -> str:
    """The client's input as text, for TripPlan's query columns."""
    return value if isinstance(value, str) else f"{value['lat']},{value['lon']}"


def _infeasible(exc: NoFeasiblePlan, line: RouteLine) -> TripInfeasible:
    """Add where on the map the vehicle gets stuck, so the client can show the gap."""
    gap = exc.next_point_mile - exc.stuck_at_mile
    lat, lon = line.point_at_mile(exc.stuck_at_mile)
    return TripInfeasible(
        f"No fuel stop is within reach after mile {exc.stuck_at_mile:.1f}: the next station "
        f"or the destination is at mile {exc.next_point_mile:.1f}, {gap:.1f} miles further.",
        {
            "stuck_at_mile": round(exc.stuck_at_mile, 1),
            "next_point_mile": round(exc.next_point_mile, 1),
            "gap_miles": round(gap, 1),
            "stuck_at": {"lat": round(lat, 5), "lon": round(lon, 5)},
        },
    )


def _location_json(location: ResolvedLocation) -> dict:
    return {
        "query": location.query,
        "label": location.label,
        "lat": round(location.lat, 6),
        "lon": round(location.lon, 6),
        "resolved_by": location.resolved_by,
    }


def _route_json(route: RouteCache) -> dict:
    return {
        "distance_miles": round(route.distance_m / METERS_PER_MILE, 1),
        "duration_hours": round(route.duration_s / 3600, 2),
        "provider": route.provider,
    }


def _fuel_stops_json(stops: list[FuelStop], matches: CorridorMatch) -> list[dict]:
    rows = []
    previous_mile = 0.0
    for sequence, stop in enumerate(stops, start=1):
        station, mile = stop.station, stop.purchase.candidate.mile
        rows.append(
            {
                "sequence": sequence,
                "opis_id": station.opis_id,
                "name": station.name,
                "address": station.address,
                "city": station.city,
                "state": station.state,
                "country": station.country,
                "lat": round(station.latitude, 6),
                "lon": round(station.longitude, 6),
                "mile_marker": round(mile, 1),
                "miles_from_previous": round(mile - previous_mile, 1),
                "distance_from_route_miles": round(matches.distance_from_route[station.opis_id], 1),
                "price_per_gallon": float(station.price_per_gallon),
                "fuel_on_arrival_gallons": round(stop.purchase.fuel_on_arrival, 3),
                "gallons": float(stop.gallons),
                "cost": float(stop.cost),
            }
        )
        previous_mile = mile
    return rows


def _summary_json(
    stops: list[FuelStop], total_miles: float, mpg: float, start_fuel: float, tank: float
) -> dict:
    total_cost = sum((stop.cost for stop in stops), Decimal(0))
    purchased = sum((stop.gallons for stop in stops), Decimal(0))
    used = total_miles / mpg
    average_price = None
    if purchased:
        average_price = float((total_cost / purchased).quantize(THOUSANDTHS, ROUND_HALF_UP))
    last_stop_mile = stops[-1].purchase.candidate.mile if stops else 0.0
    if start_fuel >= tank:
        note = f"Vehicle starts with a full tank; {FUEL_COST_NOTE}"
    else:
        note = f"Vehicle starts with {start_fuel:g} of {tank:g} gallons; {FUEL_COST_NOTE}"
    return {
        "stops": len(stops),
        "fuel_used_gallons": round(used, 3),
        "fuel_purchased_gallons": float(purchased),
        "end_fuel_gallons": round(max(0.0, start_fuel + float(purchased) - used), 3),
        "total_fuel_cost": float(total_cost),
        "average_price_paid": average_price,
        "last_leg_miles": round(total_miles - last_stop_mile, 1),
        "note": note,
    }


def _map_json(
    line: tuple[tuple[float, float], ...],
    origin: ResolvedLocation,
    destination: ResolvedLocation,
    route_json: dict,
    fuel_stops: list[dict],
) -> dict:
    """A GeoJSON FeatureCollection that pastes straight into geojson.io.

    Positions are [lon, lat], and properties.kind tells the features apart.
    """
    features = [
        _feature({"type": "LineString", "coordinates": line}, kind="route", **route_json),
        _feature(_point(origin.lat, origin.lon), kind="start", label=origin.label),
        _feature(_point(destination.lat, destination.lon), kind="finish", label=destination.label),
        *(
            _feature(_point(stop["lat"], stop["lon"]), kind="fuel_stop", **stop)
            for stop in fuel_stops
        ),
    ]
    return {"type": "FeatureCollection", "features": features}


def _feature(geometry: dict, **properties) -> dict:
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def _point(lat: float, lon: float) -> dict:
    return {"type": "Point", "coordinates": _position(lat, lon)}


def _position(lat: float, lon: float) -> tuple[float, float]:
    """A GeoJSON position, [lon, lat], rounded to 5 decimals (about 1 m). A tuple, so the
    cached route geometry cannot be changed by accident; it serializes as a JSON array."""
    return round(lon, 5), round(lat, 5)


def _ms(start: float, end: float) -> int:
    return round((end - start) * 1000)
