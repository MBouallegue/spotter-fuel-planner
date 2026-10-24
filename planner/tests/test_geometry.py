import numpy as np
import pytest

from planner.services.geometry import (
    EARTH_RADIUS_MILES,
    chord_for_miles,
    haversine_miles,
    miles_for_chord,
    resample_line,
    simplify_line,
    unit_vectors,
)

MILES_PER_DEGREE = 2 * np.pi * EARTH_RADIUS_MILES / 360  # along a great circle, ~69.09


def along_equator(lon_from: float, lon_to: float, points: int = 2) -> np.ndarray:
    """An (n, 2) (lat, lon) path on the equator, where distances are easy to check."""
    return np.column_stack([np.zeros(points), np.linspace(lon_from, lon_to, points)])


def test_haversine_new_york_to_los_angeles():
    assert haversine_miles(40.7128, -74.0060, 34.0522, -118.2437) == pytest.approx(2445, rel=0.005)


def test_resample_spaces_points_evenly_and_keeps_both_ends():
    line = resample_line(along_equator(0, 1), step_miles=0.5, total_miles=MILES_PER_DEGREE)

    steps = np.diff(line.mile)
    assert steps.max() <= 0.5
    assert steps == pytest.approx(steps[0])
    assert (line.lat[0], line.lon[0], line.mile[0]) == (0, 0, 0)
    assert (line.lon[-1], line.mile[-1]) == pytest.approx((1.0, MILES_PER_DEGREE))


def test_resample_stretches_mile_markers_to_the_provider_distance():
    # Roads are longer than the line drawn through their points; the provider's distance wins.
    line = resample_line(along_equator(0, 1, points=50), step_miles=0.5, total_miles=80.0)

    assert line.mile[-1] == pytest.approx(80.0)
    assert line.point_at_mile(40.0) == pytest.approx((0.0, 0.5))


def test_unit_vectors_lie_on_the_unit_sphere():
    xyz = unit_vectors([0, 45, -30], [0, 90, 170])

    assert np.linalg.norm(xyz, axis=1) == pytest.approx(1.0)


def test_chords_convert_back_to_great_circle_miles():
    a, b = unit_vectors([40.0, 41.0], [-100.0, -100.0])

    assert miles_for_chord(chord_for_miles(5.0)) == pytest.approx(5.0)
    assert miles_for_chord(np.linalg.norm(a - b)) == pytest.approx(
        haversine_miles(40, -100, 41, -100)
    )


def test_simplify_drops_points_that_do_not_change_the_shape():
    simple = simplify_line(along_equator(0, 10, points=10_000), max_points=2000)

    assert simple == pytest.approx(np.array([[0.0, 0.0], [0.0, 10.0]]))


def test_simplify_raises_the_tolerance_until_the_line_fits():
    teeth = np.where(np.arange(5000) % 2, 0.01, 0.0)  # a zigzag with 1 km teeth
    zigzag = np.column_stack([teeth, np.linspace(0, 10, 5000)])

    simple = simplify_line(zigzag, max_points=100)

    assert 2 <= len(simple) <= 100
    assert simple[0] == pytest.approx(zigzag[0])
    assert simple[-1] == pytest.approx(zigzag[-1])
