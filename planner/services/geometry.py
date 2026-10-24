"""Distances on a spherical Earth: accurate to about 0.5%, far better than town-level data."""

import numpy as np
from numpy.typing import ArrayLike

EARTH_RADIUS_MILES = 3958.8


def haversine_miles(lat1: ArrayLike, lon1: ArrayLike, lat2: ArrayLike, lon2: ArrayLike):
    """Great-circle distance in miles; takes floats or numpy arrays (element-wise)."""
    lat1, lon1, lat2, lon2 = (np.radians(v) for v in (lat1, lon1, lat2, lon2))
    a = (
        np.sin((lat2 - lat1) / 2) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(a))
