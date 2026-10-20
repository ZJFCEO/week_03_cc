from decimal import Decimal

import pytest

from shop.pricing import bulk_discount_rate, line_total, round_money


def test_no_discount_below_ten_items():
    assert bulk_discount_rate(9) == Decimal("0")


def test_discount_starts_at_ten_items():
    assert bulk_discount_rate(10) == Decimal("0.05")


def test_discount_starts_at_hundred_items():
    assert bulk_discount_rate(100) == Decimal("0.15")


def test_round_money_rounds_half_up():
    assert round_money(Decimal("2.345")) == Decimal("2.35")
    assert round_money(Decimal("0.125")) == Decimal("0.13")


def test_line_total_applies_bulk_discount():
    # 10 × 1.99 = 19.90，打 95 折 = 18.905 → 18.91
    assert line_total(Decimal("1.99"), 10) == Decimal("18.91")


def test_quantity_must_be_positive():
    with pytest.raises(ValueError):
        bulk_discount_rate(0)
