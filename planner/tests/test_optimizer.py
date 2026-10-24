import random

import numpy as np
import pytest
from scipy.optimize import linprog

from planner.services.optimizer import Candidate, NoFeasiblePlan, Purchase, plan_fuel_stops

TOL = 1e-6


def stations(*pairs: tuple[float, float]) -> list[Candidate]:
    """Build candidates from (mile, price) pairs; station_id is the position in the list."""
    return [Candidate(mile, price, i) for i, (mile, price) in enumerate(pairs)]


def total_cost(purchases: list[Purchase]) -> float:
    return sum(p.gallons * p.candidate.price for p in purchases)


def assert_invariants(purchases, total_miles, max_range_miles, mpg, start_fuel_gallons):
    """Replay the plan: every leg fits in one tank, fuel never goes negative, no overfill."""
    tank = max_range_miles / mpg
    fuel = tank if start_fuel_gallons is None else start_fuel_gallons
    position = 0.0
    for p in purchases:
        leg = p.candidate.mile - position
        assert leg <= max_range_miles + TOL
        fuel -= leg / mpg
        assert fuel >= -TOL
        assert p.fuel_on_arrival == pytest.approx(max(fuel, 0.0), abs=TOL)
        fuel += p.gallons
        assert p.fuel_on_arrival + p.gallons <= tank + TOL
        position = p.candidate.mile
    assert total_miles - position <= max_range_miles + TOL
    assert fuel - (total_miles - position) / mpg >= -TOL


def lp_min_cost(st, total, max_range, mpg, start_gal):
    """Exact minimum cost via LP, or None if infeasible. st: [(mile, price)]."""
    cap = max_range / mpg
    st = sorted(s for s in st if 0 <= s[0] <= total)
    n = len(st)
    if n == 0:
        return 0.0 if start_gal * mpg >= total - 1e-9 else None
    c = np.array([p for _, p in st])
    A, b = [], []
    for k, (mile, _) in enumerate(st):
        row = np.zeros(n)
        row[:k] = -1
        A.append(row)
        b.append(start_gal - mile / mpg)  # fuel on arrival >= 0
        row = np.zeros(n)
        row[: k + 1] = 1
        A.append(row)
        b.append(cap - start_gal + mile / mpg)  # tank after buying <= capacity
    A.append(-np.ones(n))
    b.append(start_gal - total / mpg)  # enough fuel to finish
    res = linprog(c, A_ub=np.array(A), b_ub=np.array(b), bounds=[(0, None)] * n, method="highs")
    return res.fun if res.status == 0 else None


# --- known answers (CLAUDE.md §12) -------------------------------------------------------


def test_worked_example():
    a, b, c, d, e = stations((120, 3.40), (380, 3.10), (520, 3.60), (760, 2.90), (980, 3.30))

    purchases = plan_fuel_stops([a, b, c, d, e], total_miles=1100)

    assert [p.candidate for p in purchases] == [b, d]
    assert [p.gallons for p in purchases] == pytest.approx([26.0, 34.0])
    assert [p.gallons * p.candidate.price for p in purchases] == pytest.approx([80.60, 98.60])
    assert total_cost(purchases) == pytest.approx(179.20)
    assert_invariants(purchases, 1100, 500, 10, None)


def test_nearest_cheaper_beats_cheapest_in_range():
    here, cheaper, cheapest = stations((0, 3.00), (100, 2.50), (400, 2.00))

    purchases = plan_fuel_stops([here, cheaper, cheapest], total_miles=400, start_fuel_gallons=0)

    assert [(p.candidate, p.gallons) for p in purchases] == [
        (here, pytest.approx(10.0)),
        (cheaper, pytest.approx(30.0)),
    ]
    assert total_cost(purchases) == pytest.approx(105.00)


def test_short_trip_on_a_full_tank_buys_nothing():
    assert plan_fuel_stops(stations((100, 3.0), (300, 2.0)), total_miles=450) == []


def test_gap_longer_than_range_is_infeasible():
    with pytest.raises(NoFeasiblePlan) as exc:
        plan_fuel_stops(stations((100, 3.0), (700, 3.0)), total_miles=900)

    assert exc.value.stuck_at_mile == 100
    assert exc.value.next_point_mile == 700


# --- behaviour details --------------------------------------------------------------------


def test_equal_prices_pick_the_farthest_station():
    start, near, far = stations((0, 3.00), (200, 3.50), (300, 3.50))

    purchases = plan_fuel_stops([start, near, far], total_miles=700, start_fuel_gallons=0)

    assert [p.candidate for p in purchases] == [start, far]


def test_trip_start_cannot_buy_fuel():
    with pytest.raises(NoFeasiblePlan) as exc:
        plan_fuel_stops(stations((10, 3.0)), total_miles=100, start_fuel_gallons=0)

    assert exc.value.stuck_at_mile == 0
    assert exc.value.next_point_mile == 10


def test_destination_beyond_reach_with_no_stations():
    with pytest.raises(NoFeasiblePlan) as exc:
        plan_fuel_stops([], total_miles=600)

    assert (exc.value.stuck_at_mile, exc.value.next_point_mile) == (0, 600)


def test_stations_outside_the_route_are_ignored():
    purchases = plan_fuel_stops(
        stations((-5, 1.0), (300, 3.0), (650, 1.0)), total_miles=600, start_fuel_gallons=40
    )

    assert [p.candidate.mile for p in purchases] == [300]
    assert purchases[0].gallons == pytest.approx(20.0)


# --- property test against the exact LP optimum -------------------------------------------


def random_instance(rng: random.Random):
    max_range = rng.uniform(150, 600)
    mpg = rng.uniform(5, 15)
    total = rng.uniform(50, 2500)
    start_gal = rng.choice([None, rng.uniform(0, max_range / mpg)])
    st = [
        (round(rng.uniform(0, total), 1), round(rng.uniform(2.5, 4.5), 3))
        for _ in range(rng.randint(0, 25))
    ]
    return st, total, max_range, mpg, start_gal


def test_matches_lp_optimum_on_random_instances():
    rng = random.Random(20261024)
    feasible = infeasible = 0

    for _ in range(300):
        st, total, max_range, mpg, start_gal = random_instance(rng)
        expected = lp_min_cost(
            st, total, max_range, mpg, max_range / mpg if start_gal is None else start_gal
        )
        try:
            purchases = plan_fuel_stops(stations(*st), total, max_range, mpg, start_gal)
        except NoFeasiblePlan:
            assert expected is None
            infeasible += 1
            continue

        assert expected is not None
        assert total_cost(purchases) == pytest.approx(expected, rel=1e-6, abs=1e-6)
        assert_invariants(purchases, total, max_range, mpg, start_gal)
        feasible += 1

    # Guard against a generator that only ever produces one kind of case.
    assert feasible >= 50
    assert infeasible >= 50
