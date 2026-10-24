# Spotter fuel planner

A Django API that takes a start and a finish in the USA and returns the route, the cheapest
places to fuel up along it, and the total fuel cost, for a truck with a 500-mile range
that does 10 miles per gallon.

## Errors

Every error has the same shape: `{"error": {"code": "...", "message": "...", "details": {...}}}`.

| HTTP | code | When |
|---|---|---|
| 400 | `invalid_request` | The body or query fails validation; `details` holds the field errors |
| 400 | `same_start_and_finish` | Start and finish are less than 1 mile apart |
| 422 | `location_not_found` | A start or finish could not be found |
| 422 | `location_outside_usa` | A start or finish is outside the USA |
| 422 | `no_route` | The router found no road between the two points |
| 422 | `no_feasible_plan` | A gap between fuel points is longer than the range; `details` gives `stuck_at_mile`, `next_point_mile`, `gap_miles` and `stuck_at: {lat, lon}` |
| 429 | `throttled` | Too many requests; see `Retry-After` |
| 503 | `geocoding_unavailable` | A free-text address needs Nominatim and it is not answering; see `Retry-After` |
| 503 | `routing_unavailable` | Every routing provider failed; see `Retry-After` |

## Assumptions

- The tank starts full (50 gallons) unless `start_fuel_gallons` says otherwise.
- `total_fuel_cost` counts only the fuel bought during the trip. The fuel used is reported too.
- Any amount of fuel can be bought, and the truck may reach a station or the finish with an
  empty tank. There is no reserve.
- **Stopping is free in this model.** The plan minimizes fuel cost only, so it will add a
  short stop to save a few cents a gallon: New York to Los Angeles has 14 stops.
- Detours to stations are not modelled. Each stop reports how far it is from the route.
- Prices are static, from the CSV.
- Fuel is optimized along the router's fastest route, not across alternative routes.
- Canadian stations are used only when the route passes them. Start and finish must be in
  the USA.

## Limitations and next steps

- **Fewer stops.** Rounding prices down to a resolution before planning would cost at most
  `resolution × gallons bought` over the exact optimum, so it looked like a cheap way to
  skip stops that save fractions of a cent. On New York to Los Angeles it gives 14, 13, 13
  and 14 stops at 0, 1, 3 and 5 cents ($703.82, $703.87, $703.93, $704.13). Most stops
  follow real price steps of 3 to 8 cents on the climb into the Rockies, and rounding
  moves ties around rather than removing stops, so it was left out. Fewer stops need a
  cost per stop (or a cap on stops) in the model itself, solved with a dynamic program
  instead of the greedy rule.
