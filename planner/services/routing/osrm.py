"""OSRM, the open-source router behind router.project-osrm.org: free, no key, no SLA.

The public server allows at most one request per second and asks for an identifying
User-Agent. Its road data is © OpenStreetMap contributors.
"""

from planner.services.routing.base import (
    LatLon,
    NoRouteError,
    ProviderError,
    RouteResult,
    request_json,
)

# One route, its full-resolution geometry, and nothing we do not use.
ROUTE_PARAMS = {
    "overview": "full",
    "geometries": "polyline6",
    "steps": "false",
    "alternatives": "false",
    "annotations": "false",
}

# OSRM's codes for "no road connects these points" (NoRoute comes back as an HTTP 400).
NO_ROUTE_CODES = {"NoRoute", "NoSegment"}


class OsrmProvider:
    name = "osrm"

    def __init__(self, base_url: str, timeout: tuple[float, float]):
        self.base_url = base_url
        self.timeout = timeout

    def route(self, start: LatLon, end: LatLon) -> RouteResult:
        (lat1, lon1), (lat2, lon2) = start, end
        # OSRM takes lon,lat in the URL, the reverse of everywhere else in this codebase.
        url = f"{self.base_url}/route/v1/driving/{lon1:.6f},{lat1:.6f};{lon2:.6f},{lat2:.6f}"
        status, body = request_json(
            self.name, "GET", url, params=ROUTE_PARAMS, timeout=self.timeout
        )

        # The body's "code" says what happened, whatever the HTTP status.
        code = body.get("code") if isinstance(body, dict) else None
        if code == "Ok":
            return self._parse(body)
        if code in NO_ROUTE_CODES:
            raise NoRouteError(body.get("message") or "No road route connects these points.")
        raise ProviderError(f"osrm: HTTP {status}, code {code!r}")

    def _parse(self, body: dict) -> RouteResult:
        try:
            route = body["routes"][0]
            distance_m, duration_s = float(route["distance"]), float(route["duration"])
            geometry = route["geometry"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError(f"osrm: malformed Ok response ({exc!r})") from exc
        if not isinstance(geometry, str) or not geometry:
            raise ProviderError("osrm: Ok response without an encoded polyline")
        return RouteResult(self.name, distance_m, duration_s, geometry)
