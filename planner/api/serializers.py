"""Request validation only: types, required fields and bounds. No lookups happen here."""

from rest_framework import serializers

MAX_LOCATION_TEXT = 255  # TripPlan stores the query in a 255-character column


class CoordinatesSerializer(serializers.Serializer):
    lat = serializers.FloatField(min_value=-90, max_value=90)
    lon = serializers.FloatField(min_value=-180, max_value=180)


class LocationField(serializers.Field):
    """A place as text ("Dallas, TX", "32.77,-96.79", an address or a ZIP code), or as an
    object {"lat": <number>, "lon": <number>}."""

    # DRF runs str.format() on these messages, hence the doubled braces.
    default_error_messages = {
        "invalid": 'Expected text such as "Dallas, TX", or {{"lat": <number>, "lon": <number>}}.',
        "blank": "This field may not be blank.",
        "max_length": f"Ensure this field has no more than {MAX_LOCATION_TEXT} characters.",
    }

    def to_internal_value(self, data):
        if isinstance(data, dict):
            coordinates = CoordinatesSerializer(data=data)
            coordinates.is_valid(raise_exception=True)
            return dict(coordinates.validated_data)
        if not isinstance(data, str):
            self.fail("invalid")
        text = " ".join(data.split())
        if not text:
            self.fail("blank")
        if len(text) > MAX_LOCATION_TEXT:
            self.fail("max_length")
        return text

    def to_representation(self, value):
        return value


class TripPlanRequestSerializer(serializers.Serializer):
    start = LocationField()
    finish = LocationField()
    max_range_miles = serializers.FloatField(min_value=50, max_value=2000, default=500.0)
    mpg = serializers.FloatField(min_value=1, max_value=100, default=10.0)
    # None (the default) means the tank starts full.
    start_fuel_gallons = serializers.FloatField(min_value=0, allow_null=True, default=None)
    corridor_miles = serializers.FloatField(min_value=0.5, max_value=25, default=5.0)

    def validate(self, attrs: dict) -> dict:
        tank = attrs["max_range_miles"] / attrs["mpg"]
        start_fuel = attrs["start_fuel_gallons"]
        if start_fuel is not None and start_fuel > tank:
            raise serializers.ValidationError(
                {
                    "start_fuel_gallons": (
                        f"Must not exceed the tank size, {tank:g} gallons (max_range_miles / mpg)."
                    )
                }
            )
        return attrs
