# Geographic reference data

Station coordinates are town-level and fully offline. At request time the app reads only
the database built from these files. It never geocodes a station over the network.

| File | Contents | Source and license |
|---|---|---|
| `us_cities.csv` | 29,880 US towns: `ID, STATE_CODE, STATE_NAME, CITY, COUNTY, LATITUDE, LONGITUDE` | [kelvins/US-Cities-Database](https://github.com/kelvins/US-Cities-Database), commit `ab0961970d41f43f731314ef6f91e36606aafaac`, unmodified. MIT, see `LICENSE-us-cities.txt`. |
| `LICENSE-us-cities.txt` | The MIT license of `us_cities.csv` | Same commit. |
| `place_overrides.csv` | Towns the gazetteer lacks or gets wrong: `city, state, country, latitude, longitude, note`. `city` is spelled as in the fuel CSV. | Rows noted `nominatim:` come from [Nominatim](https://nominatim.org/): © OpenStreetMap contributors, [ODbL](https://opendatacommons.org/licenses/odbl/). Rows noted `manual:` were checked by hand. |

## How a station gets its coordinates

The key is `(normalize_town(city), state)`. The rules live in `planner/services/geodata.py`:

1. If an override exists for the key, it wins.
2. Otherwise the gazetteer row is used. When the same name appears in several counties of
   one state, the rows are averaged if they all lie within 25 miles of each other.
   Otherwise the first row is used, with a warning.
3. Anything left is stored as `unresolved` and never offered to the optimizer.

## Where the overrides came from

- **Orlando, FL (manual).** The gazetteer has a second Orlando, FL row in Brevard County,
  about 48 miles away on the coast. That row is wrong; the override uses the Orange County row.
- **San Francisco, CA (manual).** No station is in San Francisco, but a trip can start there.
  The gazetteer has a second San Francisco row at SFO, 11 miles south in San Mateo County,
  and averaging the two would put "San Francisco, CA" in Brisbane. The override is City Hall.
- **Pass Lake, ON (manual).** Nominatim's structured search has no settlement for this hamlet
  on Highway 11/17, east of Thunder Bay. The row uses the coordinates of the lake it is named
  after, which lies beside the highway. They come from one free-form Nominatim search, so
  they are also © OpenStreetMap contributors, ODbL.
- **6 US towns and 84 Canadian towns (Nominatim).** `python manage.py geocode_leftovers` looked
  up each town the gazetteer cannot place, once: a structured search, single-threaded, more
  than 1.1 s apart, with raw answers cached in `.nominatim_cache.json` (gitignored). Three
  CSV towns go by another name in OpenStreetMap; the exits in the CSV confirm the alias:
  Brookpark → Brook Park (I-480 exit 12), Elizabethport → Elizabeth (I-95 exit 13A) and
  Henrico → Sandston (I-64 exit 197A, I-295 exit 22A).

Reviewers don't need to run the helper again; its output is committed.
