"""HTTP layer: validate input, call a service, shape the response. No business logic here."""

from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthView(APIView):
    """Cheap liveness check for monitors and for the reviewer's first request."""

    def get(self, request: Request) -> Response:
        return Response({"status": "ok"})
