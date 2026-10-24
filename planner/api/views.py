"""HTTP layer: validate input, call a service, shape the response. No business logic here."""

from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from planner.models import FuelStation


class HealthView(APIView):
    """A cheap liveness check that also shows whether the station import has run."""

    def get(self, request: Request) -> Response:
        stations = FuelStation.objects.exclude(
            geocode_source=FuelStation.GeocodeSource.UNRESOLVED
        ).count()
        return Response({"status": "ok", "stations": stations})
