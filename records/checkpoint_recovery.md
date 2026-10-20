# Checkpoint 恢复记录（离线脚本模型）

每一步都是独立的 `harness_cli.py` 子进程，进程之间只通过 `.harness/offline/` 下的状态、检查点、审计文件交接。
由 `scripts/offline_demo.py` 生成，可重复执行。

run_id：`run_20261021_002229_69c388`

## 进程 1：run（脚本模型）

```bash
python harness_cli.py run --workspace workspaces/offline_demo --kind test_fix --task 'tests/ 下有失败的测试，修复 src/ 下的源码让 pytest -q 全部通过' --model script:.harness/offline_scripts/p1.json
```

退出码：`3`

```
run_id      : run_20261021_002229_69c388
status      : WAITING_APPROVAL
stop_reason : WAITING_APPROVAL
detail      : apr_0001: write_file src/shop/pricing.py
steps_used  : 6

待审批 apr_0001: write_file src/shop/pricing.py (tool_call_id=call_06)
--- a/src/shop/pricing.py
+++ b/src/shop/pricing.py
@@ -5,7 +5,7 @@
 - 所有金额保留两位小数，四舍五入（ROUND_HALF_UP），不能用银行家舍入。
 """
 
-from decimal import ROUND_HALF_EVEN, Decimal
+from decimal import ROUND_HALF_UP, Decimal
 
 CENT = Decimal("0.01")
 BULK_TIERS = ((100, Decimal("0.15")), (10, Decimal("0.05")))
@@ -15,13 +15,13 @@
     if quantity <= 0:
         raise ValueError("quantity must be positive")
     for threshold, rate in BULK_TIERS:
-        if quantity > threshold:
+        if quantity >= threshold:
             return rate
     return Decimal("0")
 
 
 def round_money(amount: Decimal) -> Decimal:
-    return amount.quantize(CENT, rounding=ROUND_HALF_EVEN)
+    return amount.quantize(CENT, rounding=ROUND_HALF_UP)
 
 
 def line_total(unit_price: Decimal, quantity: int) -> Decimal:
→ python harness_cli.py approve run_20261021_002229_69c388 apr_0001 --actor <name>
```

## 进程 2：approve

```bash
python harness_cli.py approve run_20261021_002229_69c388 apr_0001 --actor reviewer-alice
```

退出码：`0`

```
apr_0001 → GRANTED（write_file src/shop/pricing.py），执行 resume 继续
```

## 进程 3：resume（执行已批准写入，随后 Ctrl-C）

```bash
python harness_cli.py resume run_20261021_002229_69c388 --model script:.harness/offline_scripts/p3.json
```

退出码：`5`

```
run_id      : run_20261021_002229_69c388
status      : PAUSED
stop_reason : INTERRUPTED
detail      : interrupted by the user (Ctrl-C)
steps_used  : 8
```

## 进程 4：resume（继续到完成）

```bash
python harness_cli.py resume run_20261021_002229_69c388 --model script:.harness/offline_scripts/p4.json
```

退出码：`0`

```
run_id      : run_20261021_002229_69c388
status      : COMPLETED
stop_reason : COMPLETED
detail      : final answer accepted by completion check
steps_used  : 11
final_answer: 修复了 src/shop/pricing.py 的两处问题：批量折扣档位改为“达到即享受”（>=），金额舍入改为 ROUND_HALF_UP。在最终代码上运行完整测试集：12 passed。
```

## 恢复后的连续性

| 步 | 会话（进程） | tool_call_ids |
|---|---|---|
| 1 | 1 | call_01 |
| 2 | 1 | call_02 |
| 3 | 1 | call_03 |
| 4 | 1 | call_04 |
| 5 | 1 | call_05 |
| 6 | 1 | call_06 |
| 7 | 2 | call_07 |
| 8 | 2 | （被 Ctrl-C 打断） |
| 9 | 3 | call_08 |
| 10 | 3 | call_09 |
| 11 | 3 | call_10 |

每次恢复时 Harness 的判断（`run.resumed` 审计里的恢复计划）：

- 会话 2：从检查点 #6（第 6 步）恢复；待执行审批 ['apr_0001']；检查点后结束的动作 []；漂移 []；结果未知 []；恢复时计数 steps=6 tool_calls=4
- 会话 3：从检查点 #9（第 8 步）恢复；待执行审批 []；检查点后结束的动作 []；漂移 []；结果未知 []；恢复时计数 steps=8 tool_calls=5

- 整个运行中 `write_file` 成功执行 **1** 次（由 Harness 在会话 2 代为执行）；
- 会话 3 里模型重复提交的 `call_08` 被拒绝：`gate.duplicate_write` —— identical write was already applied by call_06 (act_0007) and the file has not changed since; it is not written again
- 最终状态：COMPLETED，步数 11/20，工具调用 7/60（跨三个进程累计，没有清零）

## 检查点文件

| 文件 | 步 | 原因 | 状态 | 计划项 | 证据 | 待审批 | 对话步数 |
|---|---|---|---|---|---|---|---|
| 000001.json | 1 | step_boundary | RUNNING | 3 | 0 |  | 1 |
| 000002.json | 2 | step_boundary | RUNNING | 3 | 0 |  | 2 |
| 000003.json | 3 | step_boundary | RUNNING | 3 | 0 |  | 3 |
| 000004.json | 4 | step_boundary | RUNNING | 3 | 1 |  | 4 |
| 000005.json | 5 | step_boundary | RUNNING | 3 | 2 |  | 5 |
| 000006.json | 6 | step_boundary | WAITING_APPROVAL | 3 | 2 | apr_0001 | 6 |
| 000007.json | 7 | step_boundary | RUNNING | 3 | 4 |  | 7 |
| 000008.json | 8 | step_boundary | RUNNING | 3 | 4 |  | 8 |
| 000009.json | 8 | session_end:PAUSED | PAUSED | 3 | 4 |  | 8 |
| 000010.json | 9 | step_boundary | RUNNING | 3 | 4 |  | 9 |
| 000011.json | 10 | step_boundary | RUNNING | 3 | 4 |  | 10 |
| 000012.json | 11 | step_boundary | COMPLETED | 3 | 4 |  | 11 |

进入等待审批时的检查点（节选）：

```json
{
  "run_id": "run_20261021_002229_69c388",
  "seq": 6,
  "step": 6,
  "reason": "step_boundary",
  "state.status": "WAITING_APPROVAL",
  "state.plan": [
    {
      "step": "复现失败",
      "status": "in_progress"
    },
    {
      "step": "定位并修复 pricing",
      "status": "pending"
    },
    {
      "step": "完整测试集回归",
      "status": "pending"
    }
  ],
  "state.budget": {
    "max_steps": 20,
    "max_tool_calls": 60,
    "max_tokens": null,
    "max_wall_seconds": null,
    "used_steps": 6,
    "used_tool_calls": 4,
    "used_tokens": 900,
    "used_wall_seconds": 0.26825275004375726
  },
  "state.evidence": [
    {
      "evidence_id": "ev_0001",
      "kind": "TEST_RUN",
      "path": null,
      "command": "pytest -q",
      "passed": false,
      "valid": true
    },
    {
      "evidence_id": "ev_0002",
      "kind": "FILE_READ",
      "path": "src/shop/pricing.py",
      "command": null,
      "passed": null,
      "valid": true
    }
  ],
  "pending_approvals": [
    {
      "approval_id": "apr_0001",
      "tool_call_id": "call_06",
      "tool": "write_file",
      "target": "src/shop/pricing.py",
      "target_hash": "a4fae7b79250a4abb24de2e0795e5001751f6e6582a61d55de6bab49cfcfd6b9",
      "status": "PENDING"
    }
  ],
  "memory": "7 entries",
  "workspace": "9 files",
  "checksum": "d17ef927a4738e484c261307e315362b181562c78e23f481027c7cc7f153320f"
}
```
