# How the fuel planner works

The diagrams are Mermaid: GitHub renders them, and so does VS Code with a Mermaid preview
extension. Each one is followed by its steps in plain words.

1. [Components](#1-components)
2. [A trip request, step by step](#2-a-trip-request-step-by-step)
3. [Resolving start and finish](#3-resolving-start-and-finish)
4. [Getting the route: cache, providers, circuit breaker](#4-getting-the-route-cache-providers-circuit-breaker)
5. [From the route line to candidate stations](#5-from-the-route-line-to-candidate-stations)
6. [Choosing where to fuel up](#6-choosing-where-to-fuel-up)
7. [Pricing, saving and the map page](#7-pricing-saving-and-the-map-page)
8. [Where each error comes from](#8-where-each-error-comes-from)
9. [Data model and main types](#9-data-model-and-main-types)
10. [The offline data pipeline](#10-the-offline-data-pipeline)
11. [Caches at a glance](#11-caches-at-a-glance)

---

## 1. Components

```mermaid
flowchart TB
    client["Client<br/>Postman, curl, a browser"]

    subgraph api["planner/api: HTTP only"]
        direction LR
        views["views.py<br/>health, plan, saved plan, map page"]
        ser["serializers.py<br/>types and bounds"]
        err["errors.py<br/>every exception to a status and a code"]
    end

    subgraph services["planner/services: all the logic"]
        tp["trip_planner.py<br/>runs the steps in order, builds the response"]
        loc["locations.py<br/>input to a US point"]
        rsvc["routing/<br/>route cache, OSRM and ORS,<br/>circuit breaker, throttle"]
        geom["geometry.py<br/>resample, simplify"]
        match["station_index.py + matching.py<br/>stations near the route"]
        opt["optimizer.py<br/>where to buy fuel"]
        tp --> loc & rsvc & geom & match & opt
    end

    subgraph storage["Storage"]
        direction LR
        db[("SQLite<br/>Place, FuelStation,<br/>RouteCache, TripPlan")]
        cache[("Django cache<br/>geocodes, breaker state,<br/>throttles")]
    end

    subgraph external["Free external services"]
        direction LR
        osrmapi["OSRM"]
        orsapi["openrouteservice<br/>with a key"]
        nom["Nominatim<br/>addresses only"]
        tiles["OSM map tiles"]
    end

    client --> views
    views --> ser & tp
    services --> storage
    rsvc --> osrmapi & orsapi
    loc --> nom
    client -. "map page" .-> tiles
```

- **The API layer only speaks HTTP.** Views validate the input with a serializer, call
  one service function and return its result. They hold no business logic. DRF hands
  any exception to `errors.py`, and the saved-plan and map views read `TripPlan` rows.
- **`trip_planner` is the conductor.** It calls the other services in order and builds
  the response. Each of them does one job and knows nothing about HTTP.
- **`optimizer` has no Django imports at all,** which is why the tests can check it
  against an exact linear-programming solver.
- **Only three things leave the server:** the routing call (OSRM, or ORS as the
  fallback), a Nominatim search for free-text addresses, and the map tiles, which the
  browser fetches itself.

---

## 2. A trip request, step by step

`POST /api/v1/trips/plan/` with `{"start": "New York, NY", "finish": "Los Angeles, CA"}`.
The `GET` alias does the same with query parameters.

```mermaid
sequenceDiagram
    actor Client
    participant View as TripPlanView
    participant TP as trip_planner
    participant Loc as locations
    participant RS as RoutingService
    participant Ext as OSRM or ORS
    participant Geo as route_geometry
    participant Match as matching
    participant Optimizer as optimizer
    participant DB as SQLite

    rect rgb(235, 242, 255)
    Note over Client,TP: 1. Validate
    Client->>View: POST /api/v1/trips/plan/
    View->>View: rate limit, then the serializer
    View->>TP: plan_trip(fields, base_url)
    end

    rect rgb(236, 248, 238)
    Note over TP,Loc: 2. Resolve start and finish
    TP->>Loc: resolve_location(start and finish)
    Loc->>DB: Place rows for each town
    Loc-->>TP: two US points with labels
    end

    rect rgb(255, 245, 230)
    Note over TP,Ext: 3. Get the route
    TP->>RS: get_route(origin, destination)
    RS->>DB: RouteCache lookup
    alt stored before
        DB-->>RS: the route, 0 external calls
    else first time
        RS->>Ext: one routing call
        Ext-->>RS: distance, duration, polyline
        RS->>DB: store the route
    end
    RS-->>TP: route, hit or miss, calls made
    end

    rect rgb(245, 238, 252)
    Note over TP,Optimizer: 4. Plan
    TP->>Geo: route_geometry(polyline, miles)
    Geo-->>TP: a point every 0.5 mi, a map line
    TP->>Match: stations_along_route(line, 5 mi)
    Match-->>TP: candidates sorted by mile
    TP->>Optimizer: plan_fuel_stops(candidates)
    Optimizer-->>TP: purchases, or 422 if a gap is too long
    end

    rect rgb(240, 240, 240)
    Note over Client,DB: 5. Respond
    TP->>TP: price stops, totals, GeoJSON, timings
    TP->>DB: save TripPlan
    TP-->>View: response body
    View-->>Client: 200 JSON with id and map_url
    end
```

1. **Validate.** The rate limit (60 requests a minute per client) runs before anything
   else. The serializer then checks types and bounds (range 50 to 2,000 mi, mpg 1 to 100,
   corridor 0.5 to 25 mi, start fuel no more than the tank) and fills in the defaults:
   500 mi, 10 mpg, a full tank, a 5-mile corridor. A bad request stops here with 400
   `invalid_request`.
2. **Resolve.** Each place becomes a coordinate inside the USA (section 3). "City, ST"
   costs no network call. If start and finish are less than a mile apart: 400.
3. **Route.** One call to OSRM the first time, none afterwards (section 4).
4. **Plan.** The route line becomes evenly spaced mile markers, the stations within
   5 miles of it become candidates (section 5), and the optimizer picks the purchases
   (section 6). The station index behind the matching is built once per process.
5. **Respond.** Gallons and costs are rounded exactly as shown, the response is saved
   as a `TripPlan`, and `map_url` points at its map page (section 7).

Typical timings: about 1.2 to 1.5 s for a cold New York to Los Angeles, almost all of it
the OSRM call; a few milliseconds once the route is stored.

---

## 3. Resolving start and finish

```mermaid
flowchart TD
    A["start or finish"] --> B{"coordinates?"}
    B -- "yes: an object or 'lat,lon'" --> C["use them as they are"]
    B -- no --> D{"City, ST?"}
    D -- "yes: a US state<br/>after the last comma" --> E["the town's Place rows:<br/>an override wins, else rows<br/>within 25 mi are averaged,<br/>else the first row"]
    D -- no --> H{"cached?"}
    E -- "no rows" --> H
    H -- yes --> I["the cached answer"]
    H -- no --> J["one Nominatim search, US only,<br/>cached 30 days, misses too"]
    J -- "no answer" --> K["503 geocoding_unavailable"]
    I --> L{"found?"}
    J --> L
    L -- no --> M["422 location_not_found"]
    C --> N{"in the USA?"}
    E -- found --> N
    L -- yes --> N
    N -- no --> O["422 location_outside_usa"]
    N -- yes --> P["ResolvedLocation:<br/>lat, lon, label, resolved_by"]
```

- **Cheapest first.** Coordinates need nothing, a town needs one indexed query, and only
  a street address or a ZIP code costs a Nominatim call.
- **Towns follow the stations' rules.** "City, ST" goes through the same
  `normalize_town()` and `resolve_town()` as the station import, so "St. Louis, MO" and
  "Saint Louis, missouri" land on the same point.
- **The cache key** for a free-text address is `geo:` plus a SHA-1 of the lowercased,
  space-collapsed text, so "1 Main St" and "1  main st" share one answer.
- **The US check is a set of rough boxes** around the lower 48, Alaska and Hawaii. They
  stop Paris or Mexico City, but a point just across a border gets through. Start and
  finish must both pass it.

---

## 4. Getting the route: cache, providers, circuit breaker

```mermaid
sequenceDiagram
    participant TP as trip_planner
    participant RS as RoutingService
    participant DB as RouteCache table
    participant CB as CircuitBreaker
    participant P as Provider (OSRM, then ORS)

    TP->>RS: get_route(start, end)
    RS->>RS: round both points to 4 decimals (about 11 m) and hash them
    RS->>DB: look up the key
    alt stored route
        RS->>DB: hits = hits + 1
        RS-->>TP: the route, cache_hit, 0 external calls
    else no stored route
        loop each provider in ROUTING_PROVIDERS
            RS->>CB: allow_request()
            alt breaker open
                CB-->>RS: no: skip this provider, make no call
            else breaker closed, or the one half-open trial
                RS->>P: route(start, end)
                alt a route
                    P-->>RS: RouteResult
                    RS->>CB: record_success()
                    RS->>DB: store the route
                    RS-->>TP: the route, 1 or 2 external calls
                else no road between the points
                    P-->>RS: NoRouteError (a real answer)
                    RS->>CB: record_success()
                    RS-->>TP: NoRouteError, which becomes 422 no_route
                else timeout, 5xx, 429 or a malformed body
                    P-->>RS: ProviderError, unhealthy
                    RS->>CB: record_failure()
                    Note over RS: try the next provider
                else a refusal, such as ORS's 6,000 km limit
                    P-->>RS: ProviderError, not unhealthy
                    RS->>CB: record_success()
                    Note over RS: try the next provider
                end
            end
        end
        RS-->>TP: RoutingUnavailable, which becomes 503 with Retry-After
    end
```

1. **The key** is a SHA-256 of `driving|lat,lon|lat,lon` with both points rounded to 4
   decimals, so requests for the same trip share one stored route.
2. **A stored route costs no external call.** The `hits` counter goes up, which the
   admin shows.
3. **Otherwise the providers are tried in order,** OSRM first and ORS second, ORS only
   when `ORS_API_KEY` is set. A failing provider hands over to the next instead of being
   retried, which keeps the latency predictable.
4. **"No road" is an answer, not a failure.** It is returned at once (422) and never
   failed over or counted against the provider.
5. **The call budget:** normally 1 routing call, 2 when OSRM fails and ORS answers, plus
   1 Nominatim call when an input is a free-text address.
6. **OSRM's policy is one request a second.** An outbound throttle sleeps out the rest
   of the second when two cold requests come close together.

### The circuit breaker, per provider

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Closed
    Closed --> Open: 3 failures in a row
    Open --> HalfOpen: 60 seconds pass
    HalfOpen --> Closed: the trial call succeeds
    HalfOpen --> Open: the trial call fails
```

- **Closed:** calls go through. Failures in a row are counted, and any success resets
  the count.
- **Open:** calls are skipped, so the provider is not contacted at all, and the request
  moves straight on to the next provider.
- **Half-open:** exactly one trial call goes through (`cache.add` lets only the first
  caller in); the others keep skipping until it reports back.

The breaker's state lives in Django's cache, so every thread shares it. Only the
provider's own trouble counts as a failure: a timeout, a connection error, a 5xx, a 429
or a body we cannot read. `/health` shows each provider's state: `closed`, `open`,
`half_open`, or `disabled` for ORS without a key.

---

## 5. From the route line to candidate stations

```mermaid
flowchart TB
    subgraph route["Per route, memoized"]
        direction TB
        R1["encoded polyline<br/>35,000 points, NY to LA"] --> R2["decode to (lat, lon)"]
        R2 --> R3["resample every 0.5 mi<br/>or less; each point<br/>gets its mile<br/>about 5,600 points"]
        R3 --> R4["scale the miles to<br/>the router's distance"]
        R2 --> R5["simplify to 2,000<br/>points at most,<br/>for the map"]
    end

    subgraph stations["Per process"]
        direction TB
        S1["6,738 geocoded<br/>stations"] --> S2["arrays: id, lat, lon,<br/>price, unit-sphere point"]
    end

    R4 --> M1["KD-tree over<br/>the route's points"]
    S2 --> M0["bounding box of<br/>the route + 5 mi"]
    M0 --> M2["nearest route point,<br/>if within 5 mi"]
    M1 --> M2
    M2 --> M3["mile = that point's mile<br/>off-route = arc length"]
    M3 --> M4["one per town:<br/>the cheapest"]
    M4 --> M5["candidates<br/>sorted by mile"]
```

What the corridor search sees, seen from above:

```text
   - - - - - - - - - - - - - - - - - - - - - - - - - - - -   5 mi from the route
                    B  3.1 mi off: kept                X  6 mi off: dropped
   o--o--o--o--o--o--o--o--o--o--o--o--o--o--o--o--o--o--o-->  route, a point every 0.5 mi
   0 0.5 1                 |
                           +- B's nearest point: B is a candidate at this point's mile
   - - - - - - - - - - - - - - - - - - - - - - - - - - - -   5 mi on the other side
```

1. **Resample.** The router's line has uneven spacing, from metres in cities to miles on
   the interstate. Resampling gives a point every half mile (`np.interp` over the
   cumulative distance), so each point carries a mile marker. The markers are scaled to
   the router's distance, because that is what the truck actually drives.
2. **Measure on a sphere.** Turning latitude and longitude into points on a unit sphere
   lets an ordinary KD-tree find the nearest route point. A distance of 5 miles becomes
   the chord `2 sin(5 / 2R)`, with R = 3,958.8 mi.
3. **Prefilter.** The bounding box drops most of the country with plain array
   comparisons before any distance maths. The box widens east and west by `1/cos(lat)`,
   because a degree of longitude shrinks towards the pole.
4. **Match.** One query finds every station's nearest route point within the corridor.
   That point's mile becomes the station's mile marker, and the chord becomes its
   distance off the route.
5. **One per town.** Stations are placed at their town's centre, so several can share a
   coordinate. Only the cheapest of them could ever be chosen, so the rest are dropped.

The route work takes about 60 ms the first time and is then memoized (32 routes per
process). Matching takes a few milliseconds.

---

## 6. Choosing where to fuel up

The rule, applied at every station the truck stops at:

```mermaid
flowchart TD
    S(["at the start (nothing to buy there)<br/>or at a station"]) --> R["reach = here + one full tank, 500 mi<br/>(from the start: what the tank holds)"]
    R --> Q1{"a cheaper station<br/>within reach?"}
    Q1 -- yes --> A1["buy just enough to reach<br/>the NEAREST cheaper one"]
    Q1 -- no --> Q2{"the finish<br/>within reach?"}
    Q2 -- yes --> A2(["buy just enough to finish: done"])
    Q2 -- no --> Q3{"any station<br/>within reach?"}
    Q3 -- yes --> A3["fill the tank and drive to the<br/>CHEAPEST station in reach<br/>(ties: the farthest)"]
    Q3 -- no --> A4(["NoFeasiblePlan: 422 with<br/>stuck_at_mile, next_point_mile, gap_miles"])
    A1 --> S
    A3 --> S
```

**Why this is the cheapest possible plan.** Every mile burns fuel bought somewhere in the
500 miles before it. The cheapest that mile can ever cost is the cheapest station in that
window, and this rule always pays exactly that. It is the classic "gas station problem"
(Khuller, Malekian and Mestre, *To Fill or Not to Fill*). The tests check it against an
exact linear-programming solver on 300 random trips.

**The worked example.** 1,100 miles, a full 50-gallon tank, stations A to E:

```mermaid
flowchart TB
    S0(["Start: mile 0, 50 gal"]) -- "120 mi" --> A["A: mile 120, $3.40<br/>arrive with 38 gal<br/>B is cheaper, 260 mi on<br/>38 gal is enough<br/>buy nothing"]
    A -- "260 mi" --> B["B: mile 380, $3.10<br/>arrive with 12 gal<br/>nearest cheaper: D,<br/>380 mi on<br/>buy 26 gal = $80.60"]
    B -- "380 mi, past C at $3.60" --> D["D: mile 760, $2.90<br/>arrive with 0 gal<br/>nothing cheaper,<br/>finish 340 mi on<br/>buy 34 gal = $98.60"]
    D -- "340 mi, past E at $3.30" --> F(["Finish: mile 1,100, 0 gal left"])
```

Total: **$179.20**. A, C and E are passed without buying.

**Why "nearest cheaper" and not "cheapest in range".** With an empty tank at a $3.00
station, $2.50 at +100 mi and $2.00 at +400 mi: driving straight to the cheapest
station in range costs $120, while this rule costs $105. It buys 10 gal at $3.00 to reach
the $2.50 station, then 30 gal there to reach the finish.

**What the model leaves out.** Stopping is free, so the plan will stop for a few gallons
to save a few cents a gallon (New York to Los Angeles has 14 stops). Detours to the
stations are not modelled; each stop reports how far it is from the route.

---

## 7. Pricing, saving and the map page

```mermaid
sequenceDiagram
    participant TP as trip_planner
    participant DB as TripPlan table
    actor Client
    participant Browser
    participant Map as trip_map view
    participant OSM as OSM tile servers

    TP->>TP: each stop: gallons to 3 decimals, cost = gallons x price to the cent
    TP->>TP: total = the sum of the stop costs, exactly (Decimal)
    TP->>TP: GeoJSON: route line, start, finish, one point per stop
    TP->>DB: save TripPlan(params, route, result = the body)
    TP-->>Client: 200 with id and map_url
    Client->>Browser: open map_url
    Browser->>Map: GET /api/v1/trips/{id}/map/
    Map->>DB: the TripPlan by id
    Map-->>Browser: HTML with the plan embedded (json_script)
    Browser->>OSM: map tiles, sending the page origin as Referer
    Note over Browser: Leaflet draws the GeoJSON and the summary panel.<br/>The page makes no call back to the API.
```

- **The arithmetic is checkable.** Every cost is computed from the gallons as shown, so
  a client can redo any stop, and `total_fuel_cost` equals the sum of the stops to the
  cent.
- **Plans are saved.** `GET /api/v1/trips/{id}/` returns the saved body without planning
  again, and the map page reads the same row.
- **The `map` field is plain GeoJSON** with `[lon, lat]` positions and `properties.kind`
  set to `route`, `start`, `finish` or `fuel_stop`. It pastes straight into geojson.io.

---

## 8. Where each error comes from

```mermaid
flowchart LR
    subgraph sources["Raised by"]
        ser["serializer"] --> e1["ValidationError, ParseError"]
        thr["rate limit"] --> e2["Throttled"]
        loc["locations"] --> e3["LocationNotFound"]
        loc --> e4["LocationOutsideUSA"]
        loc --> e5["GeocodingUnavailable"]
        tp["trip_planner"] --> e6["SameStartAndFinish"]
        tp --> e7["TripInfeasible"]
        rs["routing"] --> e8["NoRouteError"]
        rs --> e9["RoutingUnavailable"]
    end
    h["errors.exception_handler<br/>one shape for every error:<br/>error: code, message, details"]
    e1 & e2 & e3 & e4 & e5 & e6 & e7 & e8 & e9 --> h
    h --> s400["400 invalid_request,<br/>same_start_and_finish"]
    h --> s422["422 location_not_found, location_outside_usa,<br/>no_route, no_feasible_plan"]
    h --> s429["429 throttled"]
    h --> s503["503 geocoding_unavailable,<br/>routing_unavailable<br/>both with Retry-After"]
```

The services raise plain Python exceptions and never import from the API layer. One
table in `api/errors.py` gives each an HTTP status and a stable code that clients can
branch on.

---

## 9. Data model and main types

### Tables

```mermaid
erDiagram
    ROUTE_CACHE |o--o{ TRIP_PLAN : "reused by"
    PLACE ||..o{ FUEL_STATION : "places, at import, by town key"

    PLACE {
        string name
        string name_key "normalize_town(name)"
        string state "USPS or province code"
        string country "US or CA"
        float latitude
        float longitude
        string source "gazetteer or override"
    }
    FUEL_STATION {
        int opis_id UK
        string name "the most common spelling"
        string address
        string city
        string state
        string country
        decimal price_per_gallon "mean of the duplicate rows"
        int price_samples
        float latitude "town centre"
        float longitude
        string geocode_source
    }
    ROUTE_CACHE {
        string key UK "sha256 of the rounded points"
        string provider "osrm or ors"
        float distance_m
        float duration_s
        text polyline6 "full-resolution line"
        int hits
        datetime created_at
    }
    TRIP_PLAN {
        uuid id PK
        string start_query
        string finish_query
        json params
        json result "the exact response body"
        datetime created_at
    }
```

`Place` and `FuelStation` are not linked by a foreign key: the import gives each station
its town's coordinates once, offline, and no request geocodes a station.

### Routing types

```mermaid
classDiagram
    direction LR
    class RoutingProvider {
        <<Protocol>>
        +str name
        +route(start, end) RouteResult
    }
    class OsrmProvider {
        +OutboundThrottle throttle
        +route(start, end) RouteResult
    }
    class OrsProvider {
        +str profile
        +route(start, end) RouteResult
    }
    class RoutingService {
        +list providers
        +dict breakers
        +get_route(start, end) RouteLookup
    }
    class CircuitBreaker {
        +state() str
        +allow_request() bool
        +record_success()
        +record_failure()
    }
    class OutboundThrottle {
        +wait_turn()
    }
    class RouteResult {
        +str provider
        +float distance_m
        +float duration_s
        +str polyline6
    }
    class RouteLookup {
        +RouteCache route
        +bool cache_hit
        +int external_calls
    }
    RoutingProvider <|.. OsrmProvider
    RoutingProvider <|.. OrsProvider
    RoutingService o-- RoutingProvider
    RoutingService o-- CircuitBreaker
    OsrmProvider o-- OutboundThrottle
    RoutingProvider ..> RouteResult : returns
    RoutingService ..> RouteLookup : returns
```

### Planning types, in the order the pipeline produces them

```mermaid
classDiagram
    direction LR
    class ResolvedLocation {
        +query
        +str label
        +float lat
        +float lon
        +str resolved_by
        +int external_calls
    }
    class RouteLine {
        +ndarray lat
        +ndarray lon
        +ndarray mile
        +point_at_mile(mile)
    }
    class StationIndex {
        +ndarray ids
        +ndarray price
        +ndarray xyz
        +dict info
    }
    class CorridorMatch {
        +list~Candidate~ candidates
        +dict distance_from_route
    }
    class Candidate {
        +float mile
        +float price
        +int station_id
    }
    class Purchase {
        +Candidate candidate
        +float gallons
        +float fuel_on_arrival
    }
    class FuelStop {
        +Purchase purchase
        +StationInfo station
        +Decimal gallons
        +Decimal cost
    }
    ResolvedLocation ..> RouteLine : route between two
    RouteLine ..> CorridorMatch : matched with StationIndex
    StationIndex ..> CorridorMatch
    CorridorMatch *-- Candidate
    Candidate ..> Purchase : the optimizer chooses
    Purchase ..> FuelStop : priced with Decimal
```

---

## 10. The offline data pipeline

```mermaid
flowchart TB
    GL["geocode_leftovers<br/>one-time helper"] -- "towns nothing can place" --> N["Nominatim<br/>1 request a second, cached"]
    N -- "rows appended for review" --> O
    G["data/geo/us_cities.csv<br/>29,880 US towns, MIT"] --> IP["import_places"]
    O["data/geo/place_overrides.csv<br/>93 hand-checked towns"] --> IP
    IP --> PL[("Place")]

    F["data/raw/fuel-prices-for-be-assessment.csv<br/>8,151 rows"] --> c1
    subgraph IS["import_stations"]
        direction TB
        c1["clean: strip spaces,<br/>check id, price and state"] --> c2["merge repeated OPIS ids:<br/>mean price, most common name"]
        c2 --> c3["place each station at its town:<br/>override, else gazetteer,<br/>averaged within 25 mi"]
    end
    PL --> c3
    c3 --> FS[("FuelStation<br/>6,738 stations, 0 unresolved")]
    c3 --> rep["printed import report"]
```

1. **`import_places`** loads the gazetteer and the overrides into `Place`, in one
   transaction.
2. **`import_stations`** cleans the price file, merges the 678 repeated station ids,
   places every station at its town and prints a report. It fails if more than 0.5% of
   the stations cannot be placed.
3. **`geocode_leftovers`** was run once to find the towns the gazetteer lacks. Its
   answers are committed in `place_overrides.csv`, so a reviewer never needs it.

After an import, restart the server: the station index is built once per process.

---

## 11. Caches at a glance

| What | Where | Kept for | It saves |
|---|---|---|---|
| Routes | `RouteCache` table | for good | the routing call on every repeated trip |
| Free-text geocodes | Django cache, `geo:<sha1>` | 30 days | a Nominatim call for a repeated address |
| Station index | process memory (`functools.cache`) | the process | a database query per station |
| Route geometry | process memory (`lru_cache`, 32 routes) | the process | about 60 ms of decoding on each warm request |
| Breaker state, throttles | Django cache | short | shared by every thread of the process |
| Database connection | `CONN_MAX_AGE`, 600 s | 10 minutes | SQLite's file set-up on every request |
