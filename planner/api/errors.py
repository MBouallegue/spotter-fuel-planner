"""One error shape for every failure: {"error": {"code", "message", "details"}}.

Services raise plain domain exceptions and never import from the API layer. This table
gives each one an HTTP status and a stable code that clients can branch on.
"""

from rest_framework import exceptions
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from planner.services.locations import (
    GeocodingUnavailable,
    LocationNotFound,
    LocationOutsideUSA,
)
from planner.services.routing.base import NoRouteError, RoutingUnavailable
from planner.services.trip_planner import SameStartAndFinish, TripInfeasible

DOMAIN_ERRORS: dict[type[Exception], tuple[int, str]] = {
    SameStartAndFinish: (400, "same_start_and_finish"),
    LocationNotFound: (422, "location_not_found"),
    LocationOutsideUSA: (422, "location_outside_usa"),
    NoRouteError: (422, "no_route"),
    TripInfeasible: (422, "no_feasible_plan"),
    GeocodingUnavailable: (503, "geocoding_unavailable"),
    RoutingUnavailable: (503, "routing_unavailable"),
}


def error_body(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def exception_handler(exc: Exception, context: dict) -> Response | None:
    """DRF's EXCEPTION_HANDLER: domain errors first, then DRF's own exceptions, reshaped."""
    for error_type, (status, code) in DOMAIN_ERRORS.items():
        if isinstance(exc, error_type):
            headers = None
            if isinstance(exc, RoutingUnavailable):
                headers = {"Retry-After": str(exc.retry_after_seconds)}
            return Response(
                error_body(code, str(exc), getattr(exc, "details", None)),
                status=status,
                headers=headers,
            )

    # DRF builds the response (status, headers such as Retry-After); we only reshape it.
    response = drf_exception_handler(exc, context)
    if response is None:
        return None  # a bug, not a client error: Django logs it and returns a 500
    if isinstance(exc, exceptions.ValidationError):
        response.data = error_body("invalid_request", "The request is invalid.", response.data)
    elif isinstance(exc, exceptions.ParseError):
        response.data = error_body("invalid_request", str(exc.detail))
    elif isinstance(exc, exceptions.Throttled):
        response.data = error_body("throttled", str(exc.detail), {"retry_after_seconds": exc.wait})
    else:
        # NotFound, MethodNotAllowed, UnsupportedMediaType...: keep DRF's code and message.
        detail = response.data.get("detail", "")
        response.data = error_body(getattr(detail, "code", "error"), str(detail))
    return response
