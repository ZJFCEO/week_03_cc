from decimal import Decimal

import pytest

from shop.cart import Cart
from shop.coupons import UnknownCoupon


def test_empty_cart_total_is_zero():
    assert Cart().total() == Decimal("0.00")


def test_subtotal_sums_lines():
    cart = Cart()
    cart.add("pen", "1.50", 2)
    cart.add("book", "12.00")
    assert cart.subtotal() == Decimal("15.00")


def test_fixed_coupon_needs_minimum():
    cart = Cart()
    cart.add("book", "12.00")
    assert cart.total("SAVE10") == Decimal("12.00")


def test_fixed_coupon_applies_over_minimum():
    cart = Cart()
    cart.add("lamp", "120.00")
    assert cart.total("SAVE10") == Decimal("110.00")


def test_bulk_order_with_percent_coupon():
    # 100 × 2.50 = 250.00，打 85 折 = 212.50；VIP5 再减 5% = 201.875 → 201.88
    cart = Cart()
    cart.add("cable", "2.50", 100)
    assert cart.total("VIP5") == Decimal("201.88")


def test_unknown_coupon_is_rejected():
    with pytest.raises(UnknownCoupon):
        Cart().total("FREE")
