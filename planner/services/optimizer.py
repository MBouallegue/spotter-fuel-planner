"""Cheapest fuel purchases along a fixed route (the "gas station problem").

The rule, applied at every station we stop at (a full tank covers ``max_range_miles``):

1. If a **cheaper** station is within one full tank, buy just enough to reach the
   **nearest** cheaper one.
2. Otherwise, if the **destination** is within one full tank, buy just enough to finish.
3. Otherwise, **fill up** and drive to the **cheapest** station within one full tank
   (ties go to the farthest, which means fewer stops for the same cost).
4. If no station is within reach, the trip is infeasible: raise ``NoFeasiblePlan``.

The trip start is not a station: we leave with ``start_fuel_gallons`` and cannot buy there.

Why it is optimal: every mile burns fuel bought somewhere in the ``max_range_miles``
behind it, so the cheapest that mile can ever cost is the cheapest station in that window.
This rule always pays exactly that. (Khuller, Malekian & Mestre, "To Fill or Not to Fill".)
The naive "drive to the cheapest station in range" is wrong: with an empty tank at a
$3.00 station, $2.50 at +100 mi and $2.00 at +400 mi, it pays $120 instead of $105.

Pure Python with no Django imports, so the tests can check it against an exact LP solver.
"""

from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass

# Tolerance for float comparisons on miles, gallons and prices.
EPS = 1e-9


@dataclass(frozen=True)
class Candidate:
    """A station the route passes, reduced to what the optimizer needs."""

    mile: float  # distance from the trip start along the route
    price: float  # USD per gallon
    station_id: int


@dataclass(frozen=True)
class Purchase:
    """One fuel stop: where, how much we buy, and how much was left in the tank on arrival."""

    candidate: Candidate
    gallons: float
    fuel_on_arrival: float


class NoFeasiblePlan(Exception):
    """A gap between fuel points is longer than one full tank."""

    def __init__(self, stuck_at_mile: float, next_point_mile: float):
        self.stuck_at_mile = stuck_at_mile
        self.next_point_mile = next_point_mile
        super().__init__(
            f"No fuel stop reachable after mile {stuck_at_mile:.1f}; "
            f"next station/destination is at mile {next_point_mile:.1f}"
        )


def plan_fuel_stops(
    candidates: Iterable[Candidate],
    total_miles: float,
    max_range_miles: float = 500.0,
    mpg: float = 10.0,
    start_fuel_gallons: float | None = None,
) -> list[Purchase]:
    """Return the cheapest list of purchases that gets us to ``total_miles``.

    ``start_fuel_gallons=None`` means the tank starts full.
    Raises ``NoFeasiblePlan`` when some gap is longer than the vehicle's range.
    """
    tank = max_range_miles / mpg
    fuel = tank if start_fuel_gallons is None else float(start_fuel_gallons)
    stations = sorted(
        (c for c in candidates if -EPS <= c.mile <= total_miles + EPS),
        key=lambda c: (c.mile, c.price),
    )
    miles = [s.mile for s in stations]

    here: Candidate | None = None  # None means "still at the trip start"
    here_mile = 0.0
    first_ahead = 0  # index of the first station we have not reached yet
    purchases: list[Purchase] = []

    while True:
        # At a station we can fill up; at the start we only have what is in the tank.
        reach = here_mile + (max_range_miles if here is not None else fuel * mpg)
        end = bisect_right(miles, reach + EPS, lo=first_ahead)
        ahead = range(first_ahead, end)

        # At the start any reachable station counts as "cheaper": the start sells nothing.
        cheaper = next(
            (k for k in ahead if here is None or stations[k].price < here.price - EPS), None
        )
        if cheaper is not None:  # rule 1
            next_index = cheaper
            gallons = (stations[cheaper].mile - here_mile) / mpg - fuel
        elif total_miles <= reach + EPS:  # rule 2
            next_index = None
            gallons = (total_miles - here_mile) / mpg - fuel
        elif not ahead:  # rule 4
            next_point = stations[end].mile if end < len(stations) else total_miles
            raise NoFeasiblePlan(here_mile, next_point)
        else:  # rule 3
            next_index = min(ahead, key=lambda k: (stations[k].price, -stations[k].mile))
            gallons = tank - fuel

        # gallons <= 0 means we already carry enough: drive on without stopping.
        if here is not None and gallons > EPS:
            purchases.append(Purchase(here, gallons, max(fuel, 0.0)))
            fuel += gallons
        if next_index is None:
            return purchases

        nxt = stations[next_index]
        fuel = max(fuel - (nxt.mile - here_mile) / mpg, 0.0)
        here, here_mile, first_ahead = nxt, nxt.mile, next_index + 1
