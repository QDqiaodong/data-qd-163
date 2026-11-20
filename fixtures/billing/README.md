# calendar-billing 可复现输入

这些 JSON 是 `calendar-billing --db DB --source INPUT` 的逐次输入，分两条
分支在一个全新的 SQLite 账簿上执行；完整可重放流程见
`examples/demo_billing.py`。

初始账簿：结账时区 `UTC`、最早服务月 `2025-01`、房间 `R` 每月免费
`60` 分钟、超额每分钟 `2` 分。规则都是 UTC 单次预约，`2025-01-06`。

## 分支 A：清单改动后补差，之后保持安静

| 文件 | 动作与契约预期 |
| --- | --- |
| `01_init.json` | 创建账簿，返回 `{"first_month":"2025-01"}` |
| `02_replace_original.json` | A=甲@09:00、B=乙@10:00，`occurrence_count=2` |
| `03_close_january.json` | 1 月账单：A 免费 60 分钟收 0；B 免费 0 收 `120`；`adjustments=[]` |
| `04_replace_swapped.json` | 撤掉 A，补入 C=丙@11:00，保留 B；`occurrence_count=2` |
| `05_close_january_retry.json` | 重试关闭 1 月：原账单原样返回，不消耗待入账补差 |
| `06_close_february.json` | 2 月账单 `charges=[]`；1 月补差为 C `+120`、B `-120`，不得因合计不变而省略 |
| `07a_close_march_quiet.json` | 3 月账单空；不再重复列出已入账的两笔补差 |

## 分支 B：3 月关账前把清单改回原样

重放分支 A 到 `06_close_february.json`（不含 `07a`），然后：

| 文件 | 动作与契约预期 |
| --- | --- |
| `07b_replace_original_again.json` | 清单改回 A=甲@09:00、B=乙@10:00 |
| `08b_close_march_after_revert.json` | 3 月账单补差反向：C `-120`、B `+120` |
| `09_show_january.json` | 1 月原账单仍原样可查 |
| `10_show_february.json` | 2 月补差账单仍原样可查 |

所有输入显式提供月份；业务逻辑不读取系统时钟。
