"""Find the stations along a route, and the mile at which the route passes each one."""

from dataclasses import dataclass

import numpy as np
from scipy.spatial import KDTree

from planner.services.geometry import RouteLine, chord_for_miles, miles_for_chord, unit_vectors
from planner.services.optimizer import Candidate
from planner.services.station_index import StationIndex

MILES_PER_DEGREE_LAT = 69.0  # rounded down, so the bounding box errs on the wide side


@dataclass(frozen=True)
class CorridorMatch:
    candidates: list[Candidate]  # one per town, sorted by mile
    distance_from_route: dict[int, float]  # OPIS ID -> miles between station and route


def stations_along_route(
    line: RouteLine, index: StationIndex, corridor_miles: float
) -> CorridorMatch:
    """The stations within ``corridor_miles`` of the route, as optimizer candidates.

    1. Keep the stations inside the route's bounding box, widened by the corridor. Cheap,
       and it drops most of the country before any distance maths.
    2. A KD-tree over the route's unit-sphere points gives each remaining station its
       nearest route point; stations farther than the corridor are dropped.
    3. A station's mile is the mile of that nearest point.
    4. Stations with the same coordinates (the same town) collapse to the cheapest one:
       the optimizer could never choose the others.
    """
    nearby = np.flatnonzero(_in_bounding_box(line, index, corridor_miles))
    tree = KDTree(unit_vectors(line.lat, line.lon))
    chords, nearest = tree.query(
        index.xyz[nearby], distance_upper_bound=chord_for_miles(corridor_miles)
    )
    within = np.isfinite(chords)  # stations beyond the corridor come back with chord = inf

    hits = nearby[within]
    # Plain Python lists: iterating them is much faster than indexing numpy arrays.
    stations = zip(
        index.ids[hits].tolist(),
        index.price[hits].tolist(),
        index.lat[hits].tolist(),
        index.lon[hits].tolist(),
        line.mile[nearest[within]].tolist(),
        miles_for_chord(chords[within]).tolist(),
        strict=True,
    )

    # Per coordinate, keep the cheapest station; ties go to the lower OPIS ID.
    best: dict[tuple[float, float], tuple[Candidate, float]] = {}
    for opis_id, price, lat, lon, mile, off_route in stations:
        kept = best.get((lat, lon))
        if kept is None or (price, opis_id) < (kept[0].price, kept[0].station_id):
            best[(lat, lon)] = (Candidate(mile, price, opis_id), off_route)

    candidates = sorted((candidate for candidate, _ in best.values()), key=lambda c: c.mile)
    distances = {candidate.station_id: off_route for candidate, off_route in best.values()}
    return CorridorMatch(candidates, distances)


def _in_bounding_box(line: RouteLine, index: StationIndex, corridor_miles: float) -> np.ndarray:
    """Boolean mask of the stations inside the route's box, padded by the corridor."""
    pad_lat = corridor_miles / MILES_PER_DEGREE_LAT
    lat_min, lat_max = line.lat.min() - pad_lat, line.lat.max() + pad_lat
    # A degree of longitude shrinks with cos(latitude): pad for the box's poleward edge.
    pad_lon = pad_lat / np.cos(np.radians(max(abs(lat_min), abs(lat_max))))
    return (
        (index.lat >= lat_min)
        & (index.lat <= lat_max)
        & (index.lon >= line.lon.min() - pad_lon)
        & (index.lon <= line.lon.max() + pad_lon)
    )
