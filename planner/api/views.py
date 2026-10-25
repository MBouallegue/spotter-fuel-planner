"""HTTP layer: validate input, call a service, shape the response. No business logic here."""

import uuid

from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from planner.api.serializers import TripPlanRequestSerializer
from planner.models import TripPlan
from planner.services.routing.service import provider_states
from planner.services.station_index import get_station_index
from planner.services.trip_planner import plan_trip


class HealthView(APIView):
    """Liveness, the number of stations the planner will use, and each routing provider's
    circuit-breaker state ("closed", "open", "half_open" or "disabled").

    The count comes from the in-memory station index, which the first call builds: one
    health check warms a freshly started server before a demo.
    """

    def get(self, request: Request) -> Response:
        return Response(
            {
                "status": "ok",
                "stations": len(get_station_index().ids),
                "providers": provider_states(),
            }
        )


class TripPlanView(APIView):
    """Plan a trip. POST takes a JSON body; GET takes the same fields as query parameters,
    so a plan can be tried straight from a browser."""

    def post(self, request: Request) -> Response:
        return self._plan(request, request.data)

    def get(self, request: Request) -> Response:
        return self._plan(request, request.query_params)

    def _plan(self, request: Request, data) -> Response:
        serializer = TripPlanRequestSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        base_url = request.build_absolute_uri("/")
        return Response(plan_trip(**serializer.validated_data, base_url=base_url))


class TripPlanDetailView(APIView):
    """A saved plan, exactly as the planning request returned it."""

    def get(self, request: Request, plan_id: uuid.UUID) -> Response:
        return Response(get_object_or_404(TripPlan, pk=plan_id).result)


def trip_map(request: HttpRequest, plan_id: uuid.UUID) -> HttpResponse:
    """A Leaflet page for a saved plan. The plan is embedded in the page, so the page
    itself calls no backend API: only the map tiles come from OpenStreetMap."""
    plan = get_object_or_404(TripPlan, pk=plan_id)
    return render(request, "planner/map.html", {"plan": plan.result})
