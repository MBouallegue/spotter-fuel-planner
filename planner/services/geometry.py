"""Distances on a spherical Earth: accurate to about 0.5%, far better than town-level data."""

from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike
from shapely import LineString

EARTH_RADIUS_MILES = 3958.8


def haversine_miles(lat1: ArrayLike, lon1: ArrayLike, lat2: ArrayLike, lon2: ArrayLike):
    """Great-circle distance in miles; takes floats or numpy arrays (element-wise)."""
    lat1, lon1, lat2, lon2 = (np.radians(v) for v in (lat1, lon1, lat2, lon2))
    a = (
        np.sin((lat2 - lat1) / 2) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(a))


@dataclass(frozen=True)
class RouteLine:
    """A route resampled to evenly spaced points, each tagged with its mile from the start."""

    lat: np.ndarray
    lon: np.ndarray
    mile: np.ndarray

    def point_at_mile(self, mile: float) -> tuple[float, float]:
        """The (lat, lon) at a given mile, interpolated between neighbouring points."""
        lat = np.interp(mile, self.mile, self.lat)
        lon = np.interp(mile, self.mile, self.lon)
        return float(lat), float(lon)


def resample_line(path: np.ndarray, step_miles: float, total_miles: float) -> RouteLine:
    """Resample a polyline, an (n, 2) array of (lat, lon), to evenly spaced points at most
    ``step_miles`` apart.

    Mile markers come from summing haversine distances along the line, then are scaled so
    the last one equals ``total_miles``: the provider's road distance, which is what the
    vehicle actually drives.
    """
    lat, lon = path[:, 0], path[:, 1]
    segments = haversine_miles(lat[:-1], lon[:-1], lat[1:], lon[1:])
    along = np.concatenate([[0.0], np.cumsum(segments)])  # miles from the first point
    length = along[-1]

    count = max(int(np.ceil(length / step_miles)) + 1, 2)
    at = np.linspace(0.0, length, count)
    scale = total_miles / length if length > 0 else 0.0
    return RouteLine(lat=np.interp(at, along, lat), lon=np.interp(at, along, lon), mile=at * scale)


def unit_vectors(lat: ArrayLike, lon: ArrayLike) -> np.ndarray:
    """Points on the unit sphere, shape (n, 3). Straight-line distance between them grows
    with great-circle distance, so a KD-tree over them finds the nearest points on Earth."""
    lat, lon = np.radians(lat), np.radians(lon)
    return np.column_stack([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)])


def chord_for_miles(miles: float) -> float:
    """Unit-sphere straight-line distance between two points ``miles`` apart on Earth."""
    return float(2 * np.sin(miles / (2 * EARTH_RADIUS_MILES)))


def miles_for_chord(chord: ArrayLike):
    """The inverse of chord_for_miles; takes a float or a numpy array."""
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.asarray(chord) / 2)


def simplify_line(path: np.ndarray, max_points: int, tolerance: float = 0.0005) -> np.ndarray:
    """Drop the points of an (n, 2) (lat, lon) array that do not change its shape at map
    scale, and return what is left in the same form.

    Douglas-Peucker with a tolerance in degrees (0.0005 is about 50 m), doubled until at
    most ``max_points`` remain. The response stays small, and the map stays smooth.
    """
    # shapely works in (x, y), which for us is (lon, lat): hence the column flips.
    line = LineString(path[:, ::-1])
    simple = line.simplify(tolerance, preserve_topology=False)
    while len(simple.coords) > max_points:
        tolerance *= 2
        simple = line.simplify(tolerance, preserve_topology=False)
    return np.asarray(simple.coords)[:, ::-1]
