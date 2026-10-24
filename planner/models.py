"""Tables: towns for offline geocoding, fuel stations, cached routes and saved trip plans."""

import uuid

from django.db import models

METERS_PER_MILE = 1609.344


class Country(models.TextChoices):
    US = "US", "United States"
    CA = "CA", "Canada"


class Place(models.Model):
    """A town centre from the offline gazetteer or the overrides file."""

    class Source(models.TextChoices):
        GAZETTEER = "gazetteer"
        OVERRIDE = "override"

    name = models.CharField(max_length=120)
    name_key = models.CharField(max_length=120)  # normalize_town(name)
    state = models.CharField(max_length=2)  # USPS state or province code
    country = models.CharField(max_length=2, choices=Country.choices, default=Country.US)
    latitude = models.FloatField()
    longitude = models.FloatField()
    source = models.CharField(max_length=10, choices=Source.choices)

    class Meta:
        # Not unique: the gazetteer has same-name towns in one state (different counties).
        indexes = [models.Index(fields=["name_key", "state"])]

    def __str__(self) -> str:
        return f"{self.name}, {self.state}"


class FuelStation(models.Model):
    """One truck stop per OPIS ID, with its mean price and town-level coordinates."""

    class GeocodeSource(models.TextChoices):
        GAZETTEER = "gazetteer"
        OVERRIDE = "override"
        UNRESOLVED = "unresolved"

    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=120)
    state = models.CharField(max_length=2, db_index=True)
    country = models.CharField(max_length=2, choices=Country.choices)
    rack_id = models.PositiveIntegerField(null=True, blank=True)
    price_per_gallon = models.DecimalField(max_digits=6, decimal_places=3)  # mean of duplicates
    price_samples = models.PositiveSmallIntegerField(default=1)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    geocode_source = models.CharField(max_length=10, choices=GeocodeSource.choices)

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state})"


class RouteCache(models.Model):
    """One stored route per rounded start/finish pair: why repeated requests make 0 API calls."""

    key = models.CharField(max_length=64, unique=True)  # sha256 of "driving|lat,lon|lat,lon"
    provider = models.CharField(max_length=20)  # "osrm" or "ors"
    distance_m = models.FloatField()
    duration_s = models.FloatField()
    polyline6 = models.TextField()  # full-resolution geometry, encoded polyline, precision 6
    hits = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.provider} route, {self.distance_m / METERS_PER_MILE:.0f} mi"


class TripPlan(models.Model):
    """A computed plan, so map_url and GET /trips/<id>/ can re-open it without recomputing."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    route = models.ForeignKey(
        RouteCache, null=True, on_delete=models.SET_NULL, related_name="plans"
    )
    start_query = models.CharField(max_length=255)
    finish_query = models.CharField(max_length=255)
    params = models.JSONField()  # max_range_miles, mpg, start_fuel_gallons, corridor_miles
    result = models.JSONField()  # the exact response body we returned
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.start_query} -> {self.finish_query}"
