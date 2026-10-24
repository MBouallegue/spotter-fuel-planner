from decimal import Decimal

import pytest

from planner.services.geodata import (
    FuelRow,
    PlaceRow,
    clean,
    merge_by_opis_id,
    read_fuel_rows,
    resolve_town,
)


def fuel_row(opis_id, price, name="STOP", city="Tomah", state="WI"):
    return FuelRow(opis_id, name, "I-94, EXIT 143", city, state, 420, Decimal(price))


def place(latitude, longitude, source="gazetteer"):
    return PlaceRow("Twin Forks", "TWIN FORKS", "TX", "US", latitude, longitude, source)


def test_clean_strips_and_collapses_whitespace():
    assert clean("  Port   Wentworth  ") == "Port Wentworth"
    assert clean(None) == ""


def test_read_fuel_rows_cleans_fields_and_skips_invalid_rows(fixtures_dir):
    rows, skipped = read_fuel_rows(fixtures_dir / "fuel_prices_sample.csv")

    assert len(rows) == 9
    assert [message.split(":")[0] for message in skipped] == ["line 11", "line 12", "line 13"]
    tomah = [row for row in rows if row.opis_id == 101]
    assert {row.city for row in tomah} == {"Tomah"}
    assert {row.address for row in tomah} == {"I-94, EXIT 143"}


def test_merge_averages_prices_rounding_half_up():
    [station], _ = merge_by_opis_id([fuel_row(1, "3.002"), fuel_row(1, "3.003")])

    # The mean is 3.0025: half-up gives 3.003, where banker's rounding would give 3.002.
    assert station.price == Decimal("3.003")
    assert station.price_samples == 2
    assert station.price_spread == Decimal("0.001")


def test_merge_keeps_the_most_common_name_and_the_first_seen_on_ties():
    [common], _ = merge_by_opis_id(
        [
            fuel_row(1, "3", "PILOT #1"),
            fuel_row(1, "3", "PILOT TRAVEL CENTER #1"),
            fuel_row(1, "3", "PILOT TRAVEL CENTER #1"),
        ]
    )
    [tie], _ = merge_by_opis_id([fuel_row(2, "3", "B NAME"), fuel_row(2, "3", "A NAME")])

    assert common.name == "PILOT TRAVEL CENTER #1"
    assert tie.name == "B NAME"


def test_merge_reports_ids_whose_location_fields_disagree():
    _, inconsistent = merge_by_opis_id([fuel_row(1, "3"), fuel_row(1, "3", city="Elsewhere")])

    assert inconsistent == [1]


def test_province_codes_mean_canada_but_a_city_named_ontario_does_not():
    stations, _ = merge_by_opis_id(
        [
            fuel_row(1, "4.454", city="Kingston", state="ON"),
            fuel_row(2, "3.1", city="Ontario", state="OR"),
        ]
    )

    assert [station.country for station in stations] == ["CA", "US"]


def test_resolve_town_without_places_is_unresolved():
    assert resolve_town([]) is None


def test_resolve_town_uses_a_single_gazetteer_row_as_is():
    where = resolve_town([place(31.0, -97.0)])

    assert (where.latitude, where.longitude, where.rule) == (31.0, -97.0, "single")


def test_resolve_town_averages_same_name_rows_within_25_miles():
    where = resolve_town([place(31.0, -97.0), place(31.1, -97.0)])  # about 7 mi apart

    assert where.rule == "averaged"
    assert where.latitude == pytest.approx(31.05)
    assert where.longitude == pytest.approx(-97.0)


def test_resolve_town_uses_the_first_row_when_same_name_rows_are_far_apart():
    where = resolve_town([place(30.0, -97.0), place(32.0, -97.0)])  # about 138 mi apart

    assert (where.latitude, where.rule) == (30.0, "far_apart")


def test_resolve_town_prefers_an_override_to_the_gazetteer():
    where = resolve_town(
        [place(28.4988, -80.5825), place(28.5399, -81.3727), place(28.5383, -81.3792, "override")]
    )

    assert (where.latitude, where.longitude) == (28.5383, -81.3792)
    assert (where.source, where.rule, where.gazetteer_rows) == ("override", "override", 2)
