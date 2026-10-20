"""价格计算：批量折扣 + 金额舍入。

业务规则：
- 购买数量达到 10 件（含）享 5% 折扣，达到 100 件（含）享 15% 折扣；
- 所有金额保留两位小数，四舍五入（ROUND_HALF_UP），不能用银行家舍入。
"""

from decimal import ROUND_HALF_EVEN, Decimal

CENT = Decimal("0.01")
BULK_TIERS = ((100, Decimal("0.15")), (10, Decimal("0.05")))


def bulk_discount_rate(quantity: int) -> Decimal:
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    for threshold, rate in BULK_TIERS:
        if quantity > threshold:
            return rate
    return Decimal("0")


def round_money(amount: Decimal) -> Decimal:
    return amount.quantize(CENT, rounding=ROUND_HALF_EVEN)


def line_total(unit_price: Decimal, quantity: int) -> Decimal:
    gross = unit_price * quantity
    return round_money(gross * (1 - bulk_discount_rate(quantity)))
