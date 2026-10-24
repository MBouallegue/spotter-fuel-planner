"""HTTP layer: validate input, call a service, shape the response. No business logic here."""

from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from planner.api.serializers import TripPlanRequestSerializer
from planner.services.station_index import get_station_index
from planner.services.trip_planner import plan_trip


class HealthView(APIView):
    """Liveness, plus the number of stations the planner will use.

    The count comes from the in-memory station index, which the first call builds: one
    health check warms a freshly started server before a demo.
    """

    def get(self, request: Request) -> Response:
        return Response({"status": "ok", "stations": len(get_station_index().ids)})


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
