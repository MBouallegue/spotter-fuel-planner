"""openrouteservice (HeiGIT), the fallback router. It needs a free API key, which allows
2,000 routes a day, 40 a minute, and routes of up to 6,000 km.

It answers in GeoJSON ([lon, lat] positions); we re-encode the line as a precision-6
polyline, so a stored route looks the same whichever provider made it.
"""

import polyline

from planner.services.routing.base import (
    LatLon,
    NoRouteError,
    ProviderError,
    RouteResult,
    is_provider_trouble,
    request_json,
)

# ORS's codes for "no road connects these points": 2009 route not found, 2010 a point is
# too far from any road. Real answers, like OSRM's NoRoute and NoSegment.
NO_ROUTE_CODES = {2009, 2010}


class OrsProvider:
    name = "ors"

    def __init__(self, base_url: str, api_key: str, profile: str, timeout: tuple[float, float]):
        self.base_url = base_url
        self.api_key = api_key
        self.profile = profile
        self.timeout = timeout

    def route(self, start: LatLon, end: LatLon) -> RouteResult:
        (lat1, lon1), (lat2, lon2) = start, end
        status, body = request_json(
            self.name,
            "POST",
            f"{self.base_url}/v2/directions/{self.profile}/geojson",
            json={"coordinates": [[lon1, lat1], [lon2, lat2]], "instructions": False},
            headers={"Authorization": self.api_key},
            timeout=self.timeout,
        )
        if status == 200:
            return self._parse(body)

        # Errors look like {"error": {"code": 2009, "message": "..."}}; the gateway's own
        # errors (a bad key, the daily quota) are {"error": "<text>"}.
        error = body.get("error") if isinstance(body, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        if code in NO_ROUTE_CODES:
            raise NoRouteError(error.get("message") or "No road route connects these points.")
        raise ProviderError(
            f"ors: HTTP {status}, {error or 'unexpected body'}",
            unhealthy=is_provider_trouble(status),
        )

    def _parse(self, body: dict) -> RouteResult:
        try:
            feature = body["features"][0]
            summary = feature["properties"]["summary"]
            distance_m, duration_s = float(summary["distance"]), float(summary["duration"])
            # GeoJSON positions are [lon, lat] (plus elevation, when asked for).
            points = [
                (float(lat), float(lon)) for lon, lat, *_ in feature["geometry"]["coordinates"]
            ]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError(f"ors: malformed response ({exc!r})") from exc
        if len(points) < 2:
            raise ProviderError("ors: route geometry has fewer than two points")
        return RouteResult(self.name, distance_m, duration_s, polyline.encode(points, 6))
