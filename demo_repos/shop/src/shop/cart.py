"""购物车：汇总明细行，再叠加优惠券。"""

from dataclasses import dataclass, field
from decimal import Decimal

from shop.coupons import coupon_discount
from shop.pricing import line_total, round_money


@dataclass(frozen=True)
class LineItem:
    sku: str
    unit_price: Decimal
    quantity: int


@dataclass
class Cart:
    items: list[LineItem] = field(default_factory=list)

    def add(self, sku: str, unit_price: str | Decimal, quantity: int = 1) -> None:
        self.items.append(LineItem(sku, Decimal(unit_price), quantity))

    def subtotal(self) -> Decimal:
        return round_money(sum((line_total(i.unit_price, i.quantity) for i in self.items), Decimal("0")))

    def total(self, coupon: str | None = None) -> Decimal:
        subtotal = self.subtotal()
        return round_money(subtotal - coupon_discount(coupon, subtotal))
