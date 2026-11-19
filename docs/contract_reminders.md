
## 持久化日历提醒队列 v1 (`calendar-reminders`)

`calendar-reminders` 是第四个、增量式入口；前三个命令的接口与报告结构完全不变。它在一个本地 SQLite 文件中维护重复日历的**工作承诺队列**：快照替换（`sync`）、工作领取（`claim`）与完成确认（`ack`）共同维护同一条提醒的生命周期。本入口只管理本地工作承诺，不接邮件、不做网络发送、没有后台守护进程；“完成”仅指一次成功的 `ack`，不宣称任何外部 exactly-once 投递。所有时间都由输入的 `now` 与规则提供，业务逻辑从不读取系统当前时间，也不实际等待租约流逝。

### 名义日期与实际开始时间：身份先于 gap/fold 解析

每条提醒的身份永久固定为 `(schedule, nominal_date)`。`nominal_date` 是按现有规则（`local_start` 所在周的星期一锚点、`interval_weeks` 周间隔、ISO `weekdays`、含端点的 `until`）遍历出的**原始本地日期**，并且先应用 `exceptions` 排除，再把“原始日期 + 原始开始时分”交给现有 `resolve` 语义做 gap/fold 解析。因此：

* gap 策略为 `skip` 时不生成提醒；
* `shift_forward` 即使把实际本地时间推到后一天（例如 `Pacific/Apia` 2011-12-30 23:30 被推到 12-31 00:00），身份仍是名义日期 2011-12-30；
* fold 的 `earlier`/`later` 只改变 UTC 时刻，不改变身份；
* 不能从解析后的 `local_start` 或 UTC 反推名义日期，也**不**使用 `calendar-reconcile` 的 `(schedule, start_utc)` 作为本入口身份。

规则字段与校验完整沿用 `model.analyze`（`schedule`、`timezone`、`local_start`、`interval_weeks`、`weekdays`、`until`、`duration_minutes`、`exceptions`、`gap_policy`、`fold_policy`）。`gap` 的 `shift_forward` 逐分钟寻找后续首个有效时间，沿用基线最多 181 次的边界，无法解析即报错；`fold` 按 UTC 早晚选择；`duration_minutes` 仍是 UTC 上的 elapsed minutes。展开为空合法。实现把展开逻辑提取为共享内部能力 `model.plan`，`analyze` 改为在其结果上渲染原报告，旧入口输出不变。

### payload 字段与相等性

每条提醒的 payload 恰含七个字段：`schedule`、`nominal_date`、`timezone`、`local_start`、`start_utc`、`end_utc`、`due_utc`。

* `nominal_date` 为上述原始日期（`YYYY-MM-DD`）；
* `timezone` 保留规则的时区名称；
* `local_start` 为解析后实际本地时间，格式 `YYYY-MM-DDTHH:MM`；
* `start_utc`/`end_utc` 沿用展开含义；
* `due_utc = start_utc - lead_minutes` 个 elapsed minutes。

payload 比较按七个字段全部相等判断。原始规则的其他差异若不改变这七个字段，不影响已经领取的承诺（保持相同 payload 的 `sync` 可以成功）。不同 `schedule` 或不同 `nominal_date` 即使落到同一 UTC 也是不同提醒。

历史秒级时区偏移必须原样保留，不截断到分钟：`Europe/Paris` 的 `1900-01-01T09:00` 解析为 `08:50:39Z`，提前 30 分钟的 `due_utc` 是 `08:20:39Z`，时长与 lead 都按真实 UTC 时间加减。本入口不套用 reconcile 的分钟 UTC 限定。

### 输入外壳与严格校验

命令固定为 `calendar-reminders --db DB --source INPUT`，没有 `--output`；成功只向 stdout 打印一个 JSON 对象。`DB` 是持久 SQLite 文件路径，`INPUT` 根对象按 `action` 精确允许字段，不允许多余或缺少字段：

* `sync`：恰含 `action`、`now`、`schedules`；`schedules` 是数组，每项恰含 `rule`（完整日历对象）与 `lead_minutes`（0..10080 整数），`schedule` 全局唯一，至多 32 条规则。
* `claim`：恰含 `action`、`now`、`limit`（1..256 整数）、`lease_minutes`（1..1440 整数）。
* `ack`：恰含 `action`、`now`、`schedule`、`nominal_date`、`token`；`schedule`、`token` 为符合 `common.ident` 的字符串，`nominal_date` 为有效 `YYYY-MM-DD`。
* `inspect`：恰含 `action`、`now`。

整数位置拒绝 JSON 布尔；标识符沿用 `common.ident`；沿用 `common.read` 的 8 MB 上限、重复 JSON 键拒绝与非有限数值拒绝。`now` 只接受 `YYYY-MM-DDTHH:MM:SSZ`：必须是 UTC、无小数秒、无其他偏移、不能是闰秒；所有 UTC 输出采用同一格式。日期/时间加减溢出（例如租约越过 `datetime` 上限）是整次操作错误。

`sync` 至多 32 条完整规则、按应用 gap/fold 后**实际生成**的提醒数至多 8192；`skip` 与 `exceptions` 排除的实例不计数，已完成而被抑制的身份仍计数。全部规则先验证：非法尾项、超限或字段错误不能被完成墓碑、无到期工作或冲突提前结论遮住；非法输入不会初始化一个不存在的 DB。允许空快照与空展开，且 `schedules`、`weekdays`、`exceptions` 数组的排列不影响身份、排序、payload 与时钟结果。

### 数据库文件规则与时钟单调

* 合法 `sync` 可以创建不存在的数据库；其他动作对不存在或未初始化的数据库报错。已有的非本功能数据库不会被清空或覆盖（按表名识别，缺少本功能表即拒绝），不提供旧库迁移。
* `DB` 不得是符号链接；解析后的 `DB` 与 `INPUT` 路径必须不同。
* 每个操作显式提供 UTC 秒精度 `now`。成功的状态变更把它记为 `last_now`；`now < last_now` 时拒绝且不写库。租约在 `now == lease_until` 时失效（有效当且仅当 `now < lease_until`），到期判断为 `due_utc <= now`，等号即可领取。
* 同一 `now` 可多次成功操作。`inspect` 也要求 `now` 不早于 `last_now`，但它是一致读，不推进 `last_now`；`sync`、`claim`（含空结果）与 `ack`（含成功幂等重试）成功时更新 `last_now`，失败不推进。

### 快照替换、墓碑与活跃领取保护

`sync` 完整替换当前规则快照，成功后当前规则恰为本次 `schedules`，缺省规则不会被当作保留。所有未完成且无有效领取的提醒按新快照重建；已过期的领取不再保护旧内容。完成墓碑独立于快照保存，不能因空快照、同名日历移除或规则时间变更而删除：已确认身份被永久抑制再次领取，移除后重加、改时区或改 fold 都不能复活它。没有完成记录的同一身份在移除后重加可以再次领取，但必须取得新 token。

**活跃领取冲突检查。** 如果新快照使任一“尚未完成、在 `now` 仍有效”的领取所对应的完整 payload 消失或变化，整个快照被拒绝：规则、提醒状态、领取 token、完成墓碑与成功操作时钟全部保持原状，不会先更新其他规则再报错，也不能先删领取、先让它失效或先写规则来规避。检查在任何写操作之前完成，比较新快照产生的完整七字段 payload。保持相同 payload 的 `sync` 允许成功，但不延长原租约、不换 token。

### claim 与 ack 语义

`claim` 只从当前快照产生、没有完成墓碑、`due_utc <= now` 且没有有效领取的提醒中，按 `(due_utc, schedule, nominal_date)` 升序最多取 `limit` 项；相同 UTC 按字符串 Unicode 码点排序。每项 `lease_until = now + lease_minutes` 分钟。每次成功领取分配一个新的非空 token，在同一数据库整个生命周期内不得复用——即使旧身份已移除、租约已过期或进程重启（token 序列持久化在 SQLite 中）。重复 `claim` 不会重发仍有效的领取；过期后重领必须换 token。

`ack` 的规则：

* 没有完成墓碑时，必须精确匹配当前身份与当前 token 且租约仍有效，才写入墓碑（保存领取时的不可变 payload、token 与本次 `now` 作为 `completed_at`）并释放领取。未知身份、未知 token、已过期 token、属于其他身份的 token 一律报错。
* 已有完成墓碑时，仅与墓碑 token 相同的 ack 是成功幂等重试：`duplicate=true`，不修改原 payload 或 `completed_at`；不同 token 报错。成功初次确认 `duplicate=false`。
* 不要求重试 ack 发生在原租约内，也不要求该身份仍在当前快照中，但 `now` 仍须满足 `last_now` 单调约束。

### 事务边界与持久投影的正确性义务

所有状态保存在本地 SQLite。每次成功 `sync`、`claim`、`ack` 都在同一个 `BEGIN IMMEDIATE` 事务中完成检查与变更，`inspect` 在同一加锁纪律下只读、不写。WAL 模式允许一个写者与读者并发，写者之间由 SQLite 写锁串行化，并设置有限 `busy_timeout`：锁争用只等待有限时间后正常报错，不会死锁，也不会留下部分业务变更。跨进程并发领取同一提醒只能产生一个有效 token；快照替换与活跃领取检查之间没有竞态。成功操作等价于某个完整的串行次序，`inspect` 不可能读到半个 `sync` 或半个 `ack`。

当前实现选择“规则快照 + 物化提醒”双投影：

* `snapshot_rules` 保存每条已验证规则及其 `lead_minutes`，可重新派生与审计展开；
* `reminders` 物化当前快照产生的每个身份及其不可变七字段 payload；
* `leases` 保存有效/过期领取（token 唯一、`lease_until`、领取时 payload 快照）；
* `completions` 独立保存完成墓碑；`seq` 持久化全局 token 序列；`meta` 保存 `last_now`。

该投影必须维护的正确性义务：

1. 快照事务在一个 commit 内同时替换规则表与物化表，二者永远描述同一代；
2. 新 payload 在触碰任何领取之前重算并完成活跃租约冲突检查，冲突时整笔回滚（替换 `reminders` 期间对 `leases` 的外键采用 `defer_foreign_keys`，在 commit 时、新代行已存在后再校验）；
3. 墓碑按身份键控且不被快照替换重写，移除/重加/时区或 fold 变更都不能复活已完成身份；
4. `claim`、`ack`、`inspect` 都基于这些表与调用方提供的 `now` 做同一套连接判断；过期租约（等号即失效）在查询中视为无领取，无需后台清理，`inspect` 仅据此展示 `ready`/`waiting` 而不写库。

输入、计算、SQLite 或文件错误退出 2，只在 stderr 说明，不输出成功 JSON，现有数据库逻辑状态不变；被拒绝的首次 `sync` 连初始化后的数据库文件都不会留下。正常成功退出 0。

### 成功输出

stdout 恰为 `{"schema_version": 1, "source_sha256": ..., "result": ...}`，SHA-256 对应本次 INPUT 原始字节。`result` 按 `action` 精确为：

* `sync`：`action`、`last_now`、`scheduled_count`（当前完整快照的全部提醒数）、`unfinished_count`（排除完成墓碑身份，含有效领取）。
* `claim`：`action`、`last_now`、`claims`；元素恰含 `payload`、`token`、`lease_until`，顺序即领取顺序，空领取为 `[]`。
* `ack`：`action`、`last_now`、`duplicate`、`completed`；`completed` 恰含 `payload`、`token`、`completed_at`。
* `inspect`：`action`、`last_now`、`jobs`、`completed`。`jobs` 仅列当前快照内尚未完成身份，每项恰含 `payload`、`state`、`lease_until`，按 `(due_utc, schedule, nominal_date)` 排序；`state` 为 `leased`（当前有效领取）、`waiting`（无有效领取且 `due_utc > now`）、`ready`（无有效领取且 `due_utc <= now`），只有 `leased` 的 `lease_until` 非 null。`completed` 列全部历史墓碑（含已不在快照中的身份），元素同 `ack.completed`，按 `(schedule, nominal_date)` 排序。所有列表不得重复同一身份。

### 手算联动样例

UTC 日历 A、B 均为单次规则：`local_start` 日期 2024-11-04、`interval_weeks=1`、`weekdays=[1]`、`until=2024-11-04`、`duration_minutes=60`、`exceptions=[]`、`gap_policy=skip`、`fold_policy=earlier`、`lead_minutes=30`；A 初始 09:00、B 初始 11:00。

* **分支一（确认后永久抑制）。** 08:00 同步（2 条）；08:30 以 `limit=1` 领取 A，租约到 08:40；08:35 同时把 A 改 10:00、B 改 12:00 的 `sync` 整体失败，`inspect` 仍是 A@09:00、B@11:00，`last_now` 仍为 08:30；08:36 原 token ack 成功；08:37 空快照；08:38 仅重加 A@10:00（`scheduled_count=1`、`unfinished_count=0`）；09:30 领取为空；最终 `inspect` 的 `completed` 保留 A@09:00 原 payload 与 `completed_at=08:36`。
* **分支二（过期改期、token 轮换）。** 独立新 DB 同样在 08:30 领取 A 但不 ack；08:40 租约恰好到期，同步 A@10:00、B@12:00 成功，同刻旧 token ack 失败；09:30 重新领取得到新 token，payload 的 `due_utc=09:30:00Z`、`start_utc=10:00:00Z`；旧 token 不能确认新领取；新 token ack 后，相同 token 重试 `duplicate=true` 且原 `completed_at` 不变，不同 token 失败。重启进程后规则、token 失效与墓碑语义仍然成立。
* **gap/fold 边界。** `Pacific/Apia` 2011-12-30 23:30（周五）：`skip` 无提醒；`shift_forward` 得 `nominal_date=2011-12-30`、`local_start=2011-12-31T00:00`、`start_utc=2011-12-30T10:00:00Z`；确认后移除再重加不得借 12-31 身份复活；`exceptions` 含 2011-12-30 时不生成。`America/New_York` 2024-11-03 01:30 提前 30 分钟：`earlier` 为 `start_utc=05:30:00Z`、`due_utc=05:00:00Z`，05:05（租约内）把 fold 改 `later` 整批失败，05:10 租约失效后可成功变为 `06:30:00Z`/`06:00:00Z`，旧 token 无效。

### 命令与演示

```sh
.venv/bin/calendar-reminders --db run/reminders.db --source fixtures/reminders/01_sync.json
python3 -I examples/demo_reminders.py
.venv/bin/python -m unittest discover -s tests -v
```

`fixtures/reminders/` 提供分支一的逐次可复现输入（ack 的 token 由演示脚本自动衔接）；`tests/test_reminders.py` 与 `tests/test_reminders_cli.py` 用字面量写出全部预期身份、UTC 时刻与状态转换，不调用被测函数生成预期，并发用例以独立进程针对同一 SQLite 文件验证互斥。
