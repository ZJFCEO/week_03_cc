"""优惠券：满减券与折扣券。未知券码直接拒绝，不静默忽略。"""

from decimal import Decimal

# code -> (类型, 面值或折扣率, 使用门槛)
COUPONS = {
    "SAVE10": ("fixed", Decimal("10"), Decimal("100")),  # 满 100 减 10
    "VIP5": ("percent", Decimal("0.05"), Decimal("0")),  # 全场 95 折
}


class UnknownCoupon(ValueError):
    pass


def coupon_discount(code: str | None, subtotal: Decimal) -> Decimal:
    if code is None:
        return Decimal("0")
    try:
        kind, value, minimum = COUPONS[code]
    except KeyError:
        raise UnknownCoupon(code) from None
    if subtotal < minimum:
        return Decimal("0")
    return value if kind == "fixed" else subtotal * value
