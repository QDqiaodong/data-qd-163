# calendar-reminders 可复现输入（分支一联动流程）

这些 JSON 是 `calendar-reminders --db DB --source INPUT` 的逐次输入，按
`01_` 到 `09_` 的编号顺序在一个全新的 SQLite 数据库上执行；`05_ack.json`
中的 `token` 由 `02_claim.json` 的输出产生，因此完整的可重放流程见
`examples/demo_reminders.py`（它自动把领取到的 token 填入 ack）。

| 文件 | 时刻 | 动作与契约预期 |
| --- | --- | --- |
| `01_sync.json` | 08:00:00Z | 同步 A@09:00、B@11:00，lead 均 30 分钟；`scheduled_count=2`、`unfinished_count=2` |
| `02_claim.json` | 08:30:00Z | 领取到期的 A，租约到 08:40:00Z（10 分钟），返回不可复用 token |
| `03_conflicting_sync.json` | 08:35:00Z | 同时改 A→10:00、B→12:00；A 租约仍有效，整批失败、退出 2，B 不变 |
| `04_inspect_after_conflict.json` | 08:35:30Z | 只读：A@09:00 `leased`、B@11:00 `waiting`，`last_now` 仍为 08:30:00Z |
| `05_ack.json`（由演示脚本生成） | 08:36:00Z | 用领取 token 确认 A，`duplicate=false`，`completed_at=08:36:00Z` |
| `06_empty_sync.json` | 08:37:00Z | 空快照；完成墓碑保留 |
| `07_readd_a_ten.json` | 08:38:00Z | 仅重加 A@10:00；`scheduled_count=1`、`unfinished_count=0`（被墓碑抑制） |
| `08_claim_after_readd.json` | 09:30:00Z | 领取结果必须为空 `[]` |
| `09_inspect_final.json` | 09:31:00Z | `jobs=[]`，`completed` 保留 A@09:00 原 payload 与 08:36:00Z 的 `completed_at` |

所有时间均为输入显式提供，业务逻辑不读取系统时钟，也不等待租约流逝。
