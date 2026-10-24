import pytest

from planner.services.normalize import normalize_town


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("St. Louis", "ST LOUIS"),
        ("Saint-Liboire", "ST LIBOIRE"),
        (" Tomah ", "TOMAH"),
        ("Sainte-Agathe", "STE AGATHE"),
        ("Fort Worth", "FT WORTH"),
        ("Mount Vernon", "MT VERNON"),
        ("Mountain Home", "MTN HOME"),
        ("Coeur d'Alene", "COEUR DALENE"),
        ("Winston-Salem", "WINSTON SALEM"),
    ],
)
def test_normalize_town(raw, expected):
    assert normalize_town(raw) == expected
