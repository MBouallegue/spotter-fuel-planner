# Spotter fuel planner

A Django API that takes a start and a finish in the USA and returns the route, the
cheapest places to fuel up along it, and the total fuel cost, for a truck with a
500-mile range that does 10 miles per gallon.

New York to Los Angeles: 2,800.6 miles, 14 fuel stops, **$703.82**. The first request makes
one routing call and takes about 1.4 s; repeating it makes no external call and takes
about 6 ms in the server.

Stack: Python 3.13, Django 6.1, Django REST framework 3.18, SQLite, numpy and scipy
(KD-tree), shapely, the public OSRM router, Leaflet for the map page.

## Quick start

Python 3.12 or newer. On macOS or Linux:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py import_places      # 29,880 US towns + 93 hand-checked overrides
python manage.py import_stations    # 6,738 stations; prints an import report
python manage.py runserver --nothreading
```

On Windows (PowerShell), the first and third lines become
`python -m venv .venv; .venv\Scripts\Activate.ps1` and `copy .env.example .env`.

`--nothreading` keeps one database connection for the whole session. Plain `runserver`
works too, but opens a new SQLite connection per request, which adds 50 to 300 ms on
Windows. Restart the server after re-importing stations: they are loaded into memory once.

Check that it is up. The first call also loads the stations into memory:

```bash
curl http://127.0.0.1:8000/api/v1/health/
# {"status": "ok", "stations": 6738, "providers": {"osrm": "closed", "ors": "disabled"}}
```

## Try it

```bash
curl -X POST http://127.0.0.1:8000/api/v1/trips/plan/ \
     -H "Content-Type: application/json" \
     -d '{"start": "New York, NY", "finish": "Los Angeles, CA"}'
```

The same thing from a browser:
`http://127.0.0.1:8000/api/v1/trips/plan/?start=New York, NY&finish=Los Angeles, CA`.
The response's `map_url` opens the plan on a map.

**Postman:** import `postman/spotter-fuel-planner.postman_collection.json` and run the
requests in order. There are 11, each with tests: health, five trips, a repeat served
from the cache, an infeasible trip, an invalid request, then the saved plan and its map.
`base_url` defaults to `http://127.0.0.1:8000`.

| Endpoint | What it does |
|---|---|
| `POST /api/v1/trips/plan/` | Plan a trip (JSON body) |
| `GET /api/v1/trips/plan/?start=…&finish=…` | The same, with query parameters |
| `GET /api/v1/trips/<id>/` | A saved plan, as it was returned |
| `GET /api/v1/trips/<id>/map/` | The saved plan on a Leaflet map |
| `GET /api/v1/health/` | Status, station count, routing providers' state |

Request fields (only `start` and `finish` are required):

| Field | Default | Allowed |
|---|---|---|
| `start`, `finish` | | `"City, ST"`, `"City, State"`, `"lat,lon"`, `{"lat": .., "lon": ..}`, or any US address or ZIP code |
| `max_range_miles` | 500 | 50 to 2,000 |
| `mpg` | 10 | 1 to 100 |
| `start_fuel_gallons` | a full tank | 0 to the tank size (range / mpg) |
| `corridor_miles` | 5 | 0.5 to 25: how far from the route a station may be |

The response, shortened:

```json
{
  "id": "fe80dc87-f274-4061-9d54-4f79803ed37c",
  "map_url": "http://127.0.0.1:8000/api/v1/trips/fe80dc87-f274-4061-9d54-4f79803ed37c/map/",
  "start": {"query": "New York, NY", "label": "New York, NY", "lat": 40.74838, "lon": -73.996705, "resolved_by": "gazetteer"},
  "finish": {"query": "Los Angeles, CA", "label": "Los Angeles, CA", "lat": 33.973093, "lon": -118.247896, "resolved_by": "gazetteer"},
  "route": {"distance_miles": 2800.6, "duration_hours": 49.88, "provider": "osrm"},
  "vehicle": {"max_range_miles": 500.0, "mpg": 10.0, "tank_gallons": 50.0, "start_fuel_gallons": 50.0},
  "fuel_stops": [
    {"sequence": 1, "opis_id": 72445, "name": "SHEETZ #639", "address": "I-80 Exit 223",
     "city": "Youngstown", "state": "OH", "country": "US", "lat": 41.0986, "lon": -80.6474,
     "mile_marker": 392.1, "miles_from_previous": 392.1, "distance_from_route_miles": 3.8,
     "price_per_gallon": 3.059, "fuel_on_arrival_gallons": 10.792, "gallons": 35.376, "cost": 108.22},
    "… 13 more stops"
  ],
  "summary": {"stops": 14, "fuel_used_gallons": 280.062, "fuel_purchased_gallons": 230.063,
              "end_fuel_gallons": 0.001, "total_fuel_cost": 703.82, "average_price_paid": 3.059,
              "last_leg_miles": 301.4,
              "note": "Vehicle starts with a full tank; total_fuel_cost is the fuel bought during the trip."},
  "map": {"type": "FeatureCollection", "features": ["route line, start, finish, one point per stop"]},
  "meta": {"external_calls": {"routing": 1, "geocoding": 0}, "route_cache": "miss", "candidate_stations": 180,
           "timing_ms": {"total": 1446, "geocoding": 3, "routing": 1292, "planning": 151}}
}
```

`map` is plain GeoJSON (`[lon, lat]` positions) and pastes straight into geojson.io.
`total_fuel_cost` is exactly the sum of the stop costs, and each cost is the gallons shown
times the price, rounded to the cent.

## How it works

```text
 start, finish ──► resolve to US points ──► route ──────────► resample the line ──► stations within ──► cheapest ──► stops, totals,
  (any format)     offline town list,       RouteCache table,    every 0.5 mi,         5 mi (KD-tree),     purchases    GeoJSON map,
                   Nominatim for            else OSRM            each point with       one per town        (greedy      saved plan
                   addresses only           (ORS as fallback)    its mile                                  rule)
```

Every step, with sequence, flow, state and class diagrams:
[docs/architecture.md](docs/architecture.md).

- **Stations are placed offline.** 98% of the addresses in the price file are exit
  descriptions ("I-44, EXIT 283"), so each station gets its town's coordinates, once, at
  import. No request ever geocodes a station.
- **Matching is array maths.** The stations live in memory as numpy arrays. A KD-tree
  over the resampled route finds, for each station, the nearest route point; a station
  within 5 miles is on the route, at that point's mile.
- **One routing call, then none.** Routes are stored by their rounded start and finish,
  so a repeated trip calls nothing.

## The fuel-stop rule

At every station the truck stops at, with one full tank (500 miles) of reach:

1. If a **cheaper** station is within reach, buy just enough to get to the **nearest**
   cheaper one.
2. Otherwise, if the **finish** is within reach, buy just enough to finish.
3. Otherwise, **fill up** and drive to the **cheapest** station within reach.
4. If no station is within reach, the trip is infeasible: 422 with the gap's location.

**Why it is the cheapest possible plan:** every mile burns fuel bought somewhere in the
500 miles before it, so the least that mile can cost is the cheapest station in that
window, and this rule always pays exactly that. It is the classic "gas station problem"
(Khuller, Malekian and Mestre, *To Fill or Not to Fill*). The tests check it against an
exact linear-programming solver on 300 random trips.

Worked example: 1,100 miles on a full tank, past A (mile 120, $3.40), B (380, $3.10),
C (520, $3.60), D (760, $2.90) and E (980, $3.30). The plan buys 26 gallons at B ($80.60),
just enough to reach the cheaper D, then 34 gallons at D ($98.60) to finish: **$179.20**.

## Data

- The price file has 8,151 rows for 6,738 stations (6,626 US, 112 Canadian). Repeated
  station ids are merged: the mean price, the most common name.
- Towns come from [kelvins/US-Cities-Database](https://github.com/kelvins/US-Cities-Database)
  (MIT). It places 6,610 stations. The other 128 use hand-checked overrides in
  `data/geo/place_overrides.csv`, looked up once on Nominatim. Every station is placed.
- Details and sources: [data/geo/README.md](data/geo/README.md).

## External APIs and resilience

- **Call budget:** 1 routing call per new trip, 0 for a repeated one; 2 if OSRM fails and
  openrouteservice answers; plus 1 Nominatim call when an input is a street address.
- **OSRM** (the public server) needs no key; we keep to its one-request-a-second policy.
- **openrouteservice** is the fallback when `ORS_API_KEY` is set (free key, 2,000 routes a
  day). Setting `ORS_PROFILE=driving-hgv` routes as a truck. The fallback is covered by
  tests; set a key in `.env` to use it.
- Every call has a timeout (3 s to connect, 15 s to read). A provider that fails 3 times in
  a row is skipped for 60 s by its circuit breaker, then tried once more.
- Clients are limited to 60 requests a minute (`API_THROTTLE_RATE`).

## Performance

Measured on a Windows laptop with `runserver --nothreading`:

| Request | Time | External calls |
|---|---|---|
| New York to Los Angeles, first time | about 1.4 s (OSRM: about 1.2 s) | 1 |
| The same trip again | about 6 ms in the server, 11 ms end to end | 0 |
| A stored route's first request after a restart | about 100 ms (decoding the route line) | 0 |

## Tests

```bash
pip install -r requirements-dev.txt
pytest              # 124 tests, offline: HTTP is mocked
pytest -m live      # one smoke test against the real OSRM server
ruff check .
```

The optimizer is checked on known answers, on its invariants (no leg longer than the
range, never a negative tank, never overfilled), and against an exact linear-programming
solver on 300 random trips, feasible and infeasible.

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
- **Station coordinates are town centres,** so a station can be a few miles from where it
  really is. Matching exit numbers to OpenStreetMap's motorway junctions would place them
  at the exit.
- **No detour cost and no fuel reserve.** Both fit the same optimizer: a reserve shrinks
  the usable tank, and a detour adds to a station's price.
- **California has 16 stations, all in the far south,** so most trips leaving northern
  California northwards are infeasible with a 500-mile range (San Francisco to Seattle
  returns a clean 422).
- **Single-process state.** Caches and breakers live in process memory, and the database is
  SQLite. For several workers: Redis for the cache, Postgres for the tables.

## Attribution

Map data and Nominatim results © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright)
(ODbL). Routing by [OSRM](https://project-osrm.org/) (public server run by FOSSGIS) and
[openrouteservice](https://openrouteservice.org/) (HeiGIT). Town coordinates from
[kelvins/US-Cities-Database](https://github.com/kelvins/US-Cities-Database) (MIT).
