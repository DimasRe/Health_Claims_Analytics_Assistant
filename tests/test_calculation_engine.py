from decimal import Decimal

import pytest

from mcp_server.calculation_engine import (
    CalculationError,
    calculate_difference,
    format_indonesian_number,
    growth,
    percentage,
    ratio,
    round_decimal,
    sum_disjoint_groups,
    to_decimal,
    validate_group_total,
    weighted_average,
)


def test_sum_disjoint_groups_for_additive_metric():
    assert sum_disjoint_groups(
        "total_pelayanan", [318275, 61409], groups_are_disjoint=True
    ) == Decimal("379684")


def test_distinct_count_cannot_be_summed():
    with pytest.raises(CalculationError, match="tidak boleh dijumlahkan"):
        sum_disjoint_groups("total_peserta_unik", [1, 2], groups_are_disjoint=True)


def test_sum_requires_disjoint_confirmation():
    with pytest.raises(CalculationError, match="tumpang tindih"):
        sum_disjoint_groups("total_pelayanan", [1, 2])


def test_percentage_growth_ratio():
    assert percentage(1, 4) == Decimal("25")
    assert growth(120, 100) == Decimal("20")
    assert growth(80, -100) == Decimal("180")
    assert ratio(3, 2) == Decimal("1.5")


@pytest.mark.parametrize("func, args", [
    (percentage, (1, 0)),
    (growth, (1, 0)),
    (ratio, (1, 0)),
])
def test_division_by_zero_is_rejected(func, args):
    with pytest.raises(CalculationError):
        func(*args)


def test_weighted_average():
    assert weighted_average("rata_rata_biaya_verifikasi", 100, 4) == Decimal("25")
    with pytest.raises(CalculationError, match="bukan average"):
        weighted_average("total_pelayanan", 1, 1)


def test_calculate_difference():
    assert calculate_difference(
        "selisih_tagihan_verifikasi",
        {"total_biaya_tagihan": 1000, "total_biaya_verifikasi": 800},
    ) == Decimal("200")
    with pytest.raises(CalculationError, match="belum lengkap"):
        calculate_difference("selisih_tagihan_verifikasi", {"total_biaya_tagihan": 1})


def test_validate_group_total():
    result = validate_group_total(
        "total_pelayanan", [318275, 61409], 379684, groups_are_disjoint=True
    )
    assert result["valid"] is True
    assert result["difference"] == 0


@pytest.mark.parametrize("value", [None, True, "abc", float("inf"), float("nan")])
def test_to_decimal_rejects_invalid(value):
    with pytest.raises(CalculationError):
        to_decimal(value)


def test_indonesian_format_and_rounding():
    assert format_indonesian_number(1234567.891, 2) == "1.234.567,89"
    assert format_indonesian_number(379684) == "379.684"
    assert round_decimal("2.345", 2) == Decimal("2.35")
    assert round_decimal("33.33333", 2) == Decimal("33.33")
