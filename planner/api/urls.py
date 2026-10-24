from django.urls import path

from planner.api import views

app_name = "api"

urlpatterns = [
    path("health/", views.HealthView.as_view(), name="health"),
    path("trips/plan/", views.TripPlanView.as_view(), name="trip-plan"),
]
