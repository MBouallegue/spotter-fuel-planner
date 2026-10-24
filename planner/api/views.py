"""HTTP layer: validate input, call a service, shape the response. No business logic here."""

from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from planner.api.serializers import TripPlanRequestSerializer
from planner.models import FuelStation
from planner.services.trip_planner import plan_trip


class HealthView(APIView):
    """A cheap liveness check that also shows whether the station import has run."""

    def get(self, request: Request) -> Response:
        stations = FuelStation.objects.exclude(
            geocode_source=FuelStation.GeocodeSource.UNRESOLVED
        ).count()
        return Response({"status": "ok", "stations": stations})


class TripPlanView(APIView):
    """Plan a trip. POST takes a JSON body; GET takes the same fields as query parameters,
    so a plan can be tried straight from a browser."""

    def post(self, request: Request) -> Response:
        return self._plan(request.data)

    def get(self, request: Request) -> Response:
        return self._plan(request.query_params)

    def _plan(self, data) -> Response:
        serializer = TripPlanRequestSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        return Response(plan_trip(**serializer.validated_data))
