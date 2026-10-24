from django.contrib import admin

from planner.models import METERS_PER_MILE, FuelStation, Place, RouteCache, TripPlan


@admin.register(Place)
class PlaceAdmin(admin.ModelAdmin):
    list_display = ["name", "state", "country", "latitude", "longitude", "source"]
    list_filter = ["source", "country"]
    search_fields = ["name", "name_key"]


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = [
        "opis_id",
        "name",
        "city",
        "state",
        "country",
        "price_per_gallon",
        "price_samples",
        "geocode_source",
    ]
    list_filter = ["country", "geocode_source", "state"]
    search_fields = ["name", "city", "=opis_id"]


@admin.register(RouteCache)
class RouteCacheAdmin(admin.ModelAdmin):
    list_display = ["id", "provider", "distance_miles", "duration_hours", "hits", "created_at"]
    list_filter = ["provider"]
    exclude = ["polyline6"]  # thousands of characters; useless in a form

    @admin.display(description="miles")
    def distance_miles(self, obj: RouteCache) -> str:
        return f"{obj.distance_m / METERS_PER_MILE:,.1f}"

    @admin.display(description="hours")
    def duration_hours(self, obj: RouteCache) -> str:
        return f"{obj.duration_s / 3600:.1f}"


@admin.register(TripPlan)
class TripPlanAdmin(admin.ModelAdmin):
    list_display = ["id", "start_query", "finish_query", "route", "created_at"]
    search_fields = ["start_query", "finish_query"]
