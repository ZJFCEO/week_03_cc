# shop

一个很小的下单计价库，用来演示 Guarded Harness 的两类任务。

- `src/shop/pricing.py`：批量折扣与金额舍入
- `src/shop/coupons.py`：优惠券（满减、折扣）
- `src/shop/cart.py`：购物车，汇总明细并叠加优惠券

业务规则：
- 购买数量达到 10 件（含）打 95 折，达到 100 件（含）打 85 折；
- 所有金额保留两位小数，四舍五入（ROUND_HALF_UP），不使用银行家舍入。

运行测试：`pytest -q`
