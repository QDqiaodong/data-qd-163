# calendar-feed 可复现输入（甲乙两组授权撤销流程）

这三个 JSON 是 `calendar-feed publish --source INPUT --output FILE` 的依次输入，
演示契约中的验收样例：别名 `meeting`、UTC 单次 `2025-01-15T09:00:00Z` 至
`10:00:00Z`、标题 `内部主题` 的活动同时属于发布组 `alpha` 与 `beta`；先撤去
`alpha` 的 detail 授权而保留 `beta` 的 busy 授权，再撤去 `beta` 的全部授权。
接收方用 `calendar-feed apply --view VIEW --delta PATCH` 逐次更新本地保存的视图。

| 文件 | 动作与契约预期 |
| --- | --- |
| `01_publish_full.json` | `previous=null`，发布全量视图：唯一一行带 `title` 的 detail 行；`view_sha256=a1f636e6…c922b9` |
| `02_publish_delta_busy.json` | `alpha` 降为 `none`：差量恰为一条无 `title` 的完整 upsert，`withdrawals=[]`；`target_sha256=c970c11a…b74dcc` |
| `03_publish_delta_hidden.json` | `beta` 也降为 `none`：差量只有 `meeting` 对应的撤下，`upserts=[]`；`target_sha256=d285184d…259c871` |

重放（在仓库根目录，视图保存在 `run/`）：

```sh
PYTHONPATH=src python3 -m calendar_expand.feed_cli publish --source fixtures/feed/01_publish_full.json --output run/view.json
PYTHONPATH=src python3 -m calendar_expand.feed_cli publish --source fixtures/feed/02_publish_delta_busy.json --output run/delta1.json
PYTHONPATH=src python3 -m calendar_expand.feed_cli apply --view run/view.json --delta run/delta1.json
PYTHONPATH=src python3 -m calendar_expand.feed_cli publish --source fixtures/feed/03_publish_delta_hidden.json --output run/delta2.json
PYTHONPATH=src python3 -m calendar_expand.feed_cli apply --view run/view.json --delta run/delta2.json
```

第一步后视图含标题行；第三步后只剩占用时间行；第五步后 `rows` 为空。
`examples/demo_feed.py` 自动重放同一流程并额外演示一次指纹不符的拒绝。
