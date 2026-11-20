# Zoned weekly schedule expansion v1

Expand one local weekly recurrence with schedule id, IANA timezone, naive minute-precision local_start, interval_weeks 1..52, ISO weekdays 1..7, inclusive until within 3660 days, duration_minutes 1..10080, and excluded local dates. The recurrence is anchored to the Monday week containing local_start; only dates on/after local_start that match interval and weekday are emitted.

Nonexistent wall times follow gap_policy skip or shift_forward (advance minute-by-minute to the first valid local minute). Ambiguous wall times follow fold_policy earlier or later, chosen by UTC instant. Duration is elapsed time after conversion to UTC, so DST transitions do not distort it. Output records both resolved local start and UTC start/end.

The synthetic New York fixture covers a spring gap and autumn fold. This is a single recurrence rule, not an RFC 5545 parser, shared-resource booking system, reminder service or legal calendar.

## Shared-resource admission v1 (`calendar-admit`)

`calendar-admit` is a second, additive entry point. `calendar-expand` and its
report shape are unchanged.

### Input

The JSON root has exactly `resources` and `requests`, both arrays.

* `resources[]` has exactly `resource` (identifier) and `capacity`
  (integer 1..1000000). Resource names are unique; at most 16 resources.
* `requests[]` has exactly `rule`, `priority` (integer 1..1000000),
  `mandatory` (JSON boolean; integers are not accepted) and nonempty
  `demands`. At most 24 requests.
* `demands[]` has exactly `resource` (identifier, must exist) and `units`
  (integer 1..1000000). A request may not list the same resource twice.
* `rule` is the complete expansion object above. `rule.schedule` is the
  request id, it is globally unique, and it must expand to at least one
  occurrence. The same bounds and DST policies apply.

Identifiers follow `common.ident`. Unknown fields, non-integers, booleans in
integer positions and nonfinite JSON numbers are input errors (exit 2). Both
arrays may be empty. Every resource and every request, including its rule, is
validated before any feasibility conclusion, so an infeasible mandatory load
never hides a malformed trailing request.

`source_sha256` is computed over the exact input bytes and `schema_version`
stays 1, as in the expansion report.

### Occupancy semantics

An occurrence occupies the UTC half-open interval `[start_utc, end_utc)` with
elapsed-minute duration. On each demanded resource a request adds `units` per
occurrence. Overlapping instances inside one request add up; they are never
unioned first. Two occurrences that only share an endpoint (`a.end == b.start`)
do not overlap.

Resources are independent, but all constraints must hold simultaneously: a
request demanding two resources consumes capacity on both for the same
intervals.

### Result

`result` has exactly `feasible`, `accepted_ids`, `rejected_ids`,
`total_priority`, `rejections`, `mandatory_conflict`.

* Feasible: `feasible=true`, every request id appears in exactly one of
  `accepted_ids` or `rejected_ids`, every list is sorted by Unicode code point
  with no duplicates, and `total_priority` is the sum of each request's
  priority counted once, mandatory or not. `mandatory_conflict` is `null`.
* `rejections` is ordered by `request_id` and has one entry per rejected
  request: `{request_id, witness}`. Only optional requests can be rejected in
  a feasible answer.
* Infeasible: all mandatory requests cannot fit together. The business answer
  is `feasible=false`, empty `accepted_ids`, all request ids in
  `rejected_ids`, `total_priority=null`, empty `rejections`, and one
  `mandatory_conflict` witness. This is a successful computation (exit 0), not
  an input error.
* Empty request set: feasible, `total_priority=0`, all lists empty,
  `mandatory_conflict=null`.

A `witness` has exactly `start_utc`, `end_utc`, `resource`, `load`,
`capacity`, `active_ids`. Times are UTC minute precision
`YYYY-MM-DDTHH:MM:00Z` and every interval has positive length. `load` is the
instance multiplicity (not the number of requests). `active_ids` lists only
requests with a real occurrence covering the whole segment, sorted by code
point.

### Witness construction

A rejection witness is computed by recomputing the resource timelines from
only the final accepted set plus the rejected request. On that recomputation,
the reported segment is the earliest over-capacity basic segment: smallest
`start_utc`, then smallest `resource`. Basic segments are delimited by all
occurrence endpoints of exactly that recomputed set on that resource. Using
the final set means an unrelated request's peak can never explain a rejection,
and using actual instances means self-overlap is preserved in `load`.

The mandatory conflict uses the same recomputation restricted to mandatory
requests, with the same earliest-segment rule.

### Algorithm

The optimiser works on a compiled representation:

1. For each resource, all occurrence endpoints of all demanding requests form
   the elementary segment grid. A request's coefficient on a segment is
   `units × number of its occurrences covering the segment`. Touching endpoints
   fall in different segments, so they never add load together.
2. Segments with the same *capacity signature* (the same set of
   `(request, effective units)` pairs) impose the same constraint on every
   subset, so the compiler merges them into one equivalence class. Checks and
   loads then run on class ids; repeated instances remain separate coefficient
   units, and time multiplicity lives only in the class lengths used by the
   unit-minute bound. A sweep over endpoints maintains the active signature
   incrementally, so a year of weekly occurrences collapses into a few hundred
   classes instead of hundreds of thousands of segments.
3. Mandatory requests are summed and checked for capacity. If they overflow,
   the answer is the mandatory conflict.
4. Optional requests are split into independent components by union-find over
   shared classes. Two requests can only interact by loading a common class, so
   different components add their priorities, remain independently feasible,
   and concatenate to the global lexicographic optimum.
5. Each component is solved by branch and bound. A candidate is accepted only
   if adding its class operations stays within capacity on every touched class.
   Bounds are the minimum over several valid single-constraint relaxations:
   tight classes where untouched requests add full priority and touched ones
   are packed fractionally, plus one unit-minute relaxation per resource. The
   search is exhaustive within those bounds, so each component's selection is
   globally optimal. It is deliberately not a per-request greedy or
   pairwise-conflict heuristic. Two passes run per component: a value pass in
   priority-per-demand order (only to find a strong incumbent) and, for exactly
   that optimal score, a target pass in id order that fixes the tie-break.
6. Requests with identical priority and identical class operations are
   symmetric. The search treats them as an interchangeable group with an
   "include a prefix" canonical form, which keeps both the optimum and the
   tie-break.
7. Ties: the sorted accepted-id lists are compared lexicographically by code
   point, shorter common prefix first. Within a component, the target pass
   visits requests in id order, include branch first, and the first set
   reaching the optimal score is the required list; components concatenate
   because their id sets never interact.
8. Witnesses are recomputed from the final set as described above rather than
   read off the all-request grid.

**Structure trade-off.** The compiled-class design stores, per request,
`(resource, effective units)` membership in each class. Repeated instances stay
separate coefficient units (effective units are `units × active occurrences`),
and endpoint handling is structural: a class exists only between distinct
endpoint times, so `[a, b)` and `[b, c)` never share a class. Because witnesses
are recomputed from actual instances of the final set, the reported segment and
`load` always match a naive recalculation even though the solver uses merged
classes. The alternative — maintaining and recomputing per-request intervals
for each candidate set — uses less precomputation but repeats interval
arithmetic at every node, makes each branch check O(instances) instead of
O(classes), and makes it easier to accidentally union self-overlapping
instances. Merging classes by signature is exact for selection because the
signature determines the constraint; correctness of the answer is safeguarded
by recomputing both witnesses directly from the reported set, so the compiled
structure only drives the choice of accepted ids.

### Boundaries and operations

* At most 16 resources and 24 requests; 8 MB input cap; duplicate JSON keys,
  nonfinite numbers and a 3660-day expansion bound are rejected.
* Exit 0 on a published report, including business infeasibility. Exit 2 on
  input, calculation or file errors, without replacing an existing report.
* On success the report is atomically published to `--output` and the same JSON
  is printed to stdout. Input and output paths must differ; an output symlink is
  rejected.
* Reruns are idempotent. Standard library only; IANA time zones come from the
  operating system.

### Arithmetic example (hand-checkable)

`fixtures/admission.json` uses a digestive-room capacity of 2. Monday 09:00
local time on 2024-03-04 is 14:00 UTC (America/New_York is UTC-5 then). The
first occurrences are `triage` 14:00–15:00 UTC (mandatory, priority 5), `scopy`
14:30–15:30 (priority 9, also needs surgery capacity 1), `swallow` 14:00–15:30
every 4th week (priority 7), and `motility` 14:00–16:00 every 4th week
(priority 7). On the shared 14:30–15:00 UTC digestive segment on 2024-03-04,
`triage + scopy` already load 2, so no other optional request fits with both.
`triage + swallow` loads 2 but adds only 7 against `scopy`'s 9; likewise
`triage + motility`. Taking all three optional requests would load 3 in that
segment, so `triage + scopy = 14` is strictly best. Both `swallow` and
`motility` are rejected, and each witness is the recomputed 14:30–15:00 UTC
digestive segment with load 3 (triage, scopy and the rejected request) — the
earliest over-capacity segment after adding that request, reported against the
final accepted set.

The tests state all expected values explicitly; they do not call the admission
solver to produce expectations.

### Commands

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
.venv/bin/calendar-admit --source fixtures/admission.json --output run/admission.json
.venv/bin/python -m unittest discover -s tests -v
```

## 计划执行核销 v1 (`calendar-reconcile`)

`calendar-reconcile` 是第三个、增量式入口。`calendar-expand`、`calendar-admit`
的接口与报告结构保持不变。它把计划日历实例与实际执行记录做核销，但**不挑一个
任意配对当作确定事实**：先求两级最优解释（先最大化配对数，再最小化绝对分钟偏差
之和），再报告在任意全局同等最优解释中仍可成立的全部三元组归属与未配对可能性。

### 输入

JSON 根恰含 `rules`、`observations`、`tolerance_minutes`。

* `rules` 是完整单规则对象数组（字段与校验沿用 `model.analyze`，不读取准入
  报告），至多 32 条；`schedule` 在本输入内唯一。允许展开为空的规则。
* `observations` 至多 32 条，每项恰含 `observation_id`、`timezone`、
  `local_start`、`eligible_schedules`。记录 ID 唯一；可归属的日历 ID 非空、无
  重复且必须引用已知规则。
* `tolerance_minutes` 是 0..10080 的整数；布尔值在整数位置被拒绝。
* 所有标识符遵循 `common.ident`；字段集合必须精确；所有输入先验证，不会因为
  没有可配对对象而跳过末尾的非法记录。
* `local_start` 是无偏移的分钟级墙上时间字符串，能被
  `datetime.fromisoformat` 解析，但不得带 tzinfo、秒或微秒（显式 `:00` 秒字段
  同样拒绝）。未知时区和非法时间格式都是输入错误。
* 按现有 gap/fold 策略展开的计划实例总数（归一前）至多 32。

### 实际记录的 UTC 证据候选

实际记录的 `local_start` 是无偏移墙上时间，实现把 `fold=0` 与 `fold=1` 分别做
IANA 时区的 UTC 往返验证（`local -> UTC -> local` 必须还原），以全部**不同的
有效 UTC 时刻**作为证据候选：

* 正常时刻一个候选；
* 秋季回拨时刻两个候选；
* 春季缺口时刻零个候选。

实际缺口不平移、不使整份输入失败，而作为无有效时刻的记录保留并出现在报告中。
计划规则的 `fold_policy` 绝不用于替实际证据预先选择时刻；两个 fold 候选一直
保留到全局核销与支持判定，因为其他记录竞争同一实例可能排除其中一个 fold。

### 分钟精度限定（仅本入口）

计划展开后每个实例的 `start_utc`、`end_utc`，以及每条实际记录经往返验证得到的
全部有效候选，秒和微秒都必须为 0。全部输入的此项精度验证在实例身份归一及任何
空集捷径**之前**完成；任一不满足即整份输入错误并保留旧报告，即使没有任何可配对
对象也拒绝，不截断、四舍五入或静默删除候选。例如 `Europe/Paris` 的
`1900-01-01T09:00` 对应 UTC `08:50:39`，在计划侧和实际侧都被拒绝；实际 gap 的
零候选仍是合法数据。旧入口行为不受此限定影响。

计划实例按 `(schedule, start_utc)` 归为同一个待核销义务，重复身份只输出和核销
一次，32 个总实例上限按归一后的数量检查（归一前的原始数量同样先检查）；不同
schedule 即使同一 UTC 仍是不同义务。

### 合法性与两级目标

一次核销解释由若干三元组 `(observation_id, 计划实例, 该记录的候选 UTC)` 构成。
三元组合法当且仅当：日历 ID 被该记录的 `eligible_schedules` 允许，且开始时刻
偏差绝对值不超过容差。每个实际记录和每个计划实例各至多使用一次；计划实例以
`(schedule, start_utc)` 二元组标识。时长不参与配对。

优化目标分两级，顺序固定：

1. 最大化配对数；
2. 在配对数相同的解释中，最小化所有配对的绝对分钟偏差之和。

`delta_minutes` 是 observed UTC 减计划 `start_utc` 的有符号整数分钟数，最小化
目标取其绝对值。匹配数优先于偏差：可能必须牺牲局部最近配对才能达到最大配对数；
相同最大配对数下总偏差更大的边不属于任何最优解释，因而不受支持。

### 支持语义

* 一个三元组"受支持"当且仅当它确实出现在**至少一个全局同等最优解释**中。候选
  合法、局部最近、或出现在某个最大（但非偏差最优）配对里，都不足以证明受支持。
* `can_be_unmatched` 为真当且仅当至少一个最优解释中该对象未配对。没有任何合法
  三元组的对象（含零候选 gap 记录、无实例可配的记录或实例）在每个解释中都未
  配对，因此也为真。
* 一个对象 `supports` 为空表示它在任何最优解释中都未配对；恰有一个支持且
  `can_be_unmatched=false` 才是确定归属；多支持或存在未配对可能性都保留歧义。
* 候选 UTC 是配对标签：同一对象对若两个候选都达到同样的最优目标，必须分别返回
  两个支持，不能先合并候选时刻。
* 报告两侧（observations 与 occurrences）从同一个受支持三元组集合渲染，必须呈现
  完全相同的一组事实。

### 结果外壳与排序

沿用 `schema_version=1`、`source_sha256`（原始输入字节的 SHA-256）和 `result`
外壳。`result` 恰含 `matched_count`、`total_deviation_minutes`、`observations`、
`occurrences`。没有任何配对时前两者均为 0；规则无实例、记录为空或两侧皆空都是
合法成功。

* 所有 UTC 时间统一为 `YYYY-MM-DDTHH:MM:00Z`。
* `observations` 按 `observation_id` 的 Unicode 码点排序；每项恰含
  `observation_id`、`candidates_utc`、`supports`、`can_be_unmatched`。
  `candidates_utc` 按 UTC 升序且无重复；`supports` 每项恰含 `schedule`、
  `start_utc`、`observed_utc`、`delta_minutes`，按
  `(schedule, start_utc, observed_utc)` 排序。
* `occurrences` 按 `(schedule, start_utc)` 排序；每项恰含 `schedule`、
  `start_utc`、`supports`、`can_be_unmatched`；其 `supports` 每项恰含
  `observation_id`、`observed_utc`、`delta_minutes`，按
  `(observation_id, observed_utc)` 排序。各支持列表无重复。

### 纽约联动样例（手算可核）

两条只发生一次的 `2024-11-03T01:30` 纽约计划规则，`fold_policy` 分别为
`earlier`（实例 `05:30Z`）与 `later`（实例 `06:30Z`）。第一条记录
`timezone=America/New_York, local_start=2024-11-03T01:30` 有两个候选
`05:30Z`、`06:30Z`；第二条记录 `timezone=UTC, local_start=2024-11-03T05:30`
只有候选 `05:30Z`；两条记录的 `eligible_schedules` 都含两条规则，容差 0。

* **两条记录同时核销**：最大配对数为 2，第二条记录只能配 `s-early@05:30Z`，
  于是第一条记录在所有最优解释中必须使用 `06:30Z` 配 `s-later`。四条对象的
  `can_be_unmatched` 全为 false；第一条记录的 `supports` 只有
  `(s-later, 06:30Z, 06:30Z, 0)`，`05:30Z` 候选虽合法但不出现在任何最优解释中。
* **移除第二条记录后**：最大配对数为 1。第一条记录的两个候选都受支持
  （`s-early@05:30Z` 与 `s-later@06:30Z`，delta 均为 0），记录本身
  `can_be_unmatched=false`；两个计划实例各有一个支持且
  `can_be_unmatched=true`（任一最优解释中必有一个实例空闲）。

### 算法：最小费用最大流 + 零残差 SCC

合法三元组构成观测与计划实例之间的二分图，同一对节点之间的并行弧携带不同候选
UTC 标签（费用可能不同）。网络为 `源 -> 观测 -> 实例 -> 汇`，容量均为 1，弧
费用为 `abs(delta_minutes)`。用带 Johnson 势的连续最短路增广求整数最小费用
最大流：增广保证先达到最大流，单位费用保证再达到最小总偏差，两级目标由此精确
保持。

一次最优流之后，不枚举任何解释即可刻画全部同等最优支持。最优时每条残差弧的
reduced cost 非负；取 reduced cost 恰为 0 的残差弧构成 G0。沿零 reduced cost
环推一单位流同时保持流量与费用，因此：

* 当前带流的弧属于当前最优解释，必然受支持；
* 不带流的弧受支持，当且仅当它本身是零 reduced cost 残差弧且两端在 G0 的同一
  强连通分量内（零环重路由即给出包含它的同优解释）；
* 观测能在某同优解释中未配对，当且仅当它在 G0 中从源可达；
* 实例能未配对，当且仅当它在 G0 中可到达汇；
* 无合法边的孤立对象按前述规则强制 `can_be_unmatched=true`。

最大流前提使环刻画完备：任何保持流量的重路由都闭合成残差环，包括经过源、汇
反向弧的重路由。fold 候选作为独立并行弧保留，因此同一对象对的两个候选会被独立
判定并分别输出。32×32 完全二分图（同刻 32 记录对 32 实例）返回 1024 个受支持
对象对，全程不枚举 32! 个解释，也不在同优解过多时截断支持。

**另一可行设计的取舍。** 也可以对每条候选弧施加"必须选中"约束后重新精确求解
最小费用流，逐弧询问是否仍存在同优解释，并对未配对状态再各跑一次重优化。该
设计单弧证明更直白，但要把整套最短路计算重复至多 E 次、再加上 O(V) 次未配对
重跑。本实现只付一次流求解加一次线性 Tarjan SCC 与两次 BFS；其正确性义务集中在
四点：(1) 势不变量持续保证所有残差 reduced cost 非负；(2) 环分析前确已流到真正
的最大流；(3) fold 候选保持为独立并行弧而非合并标签；(4) 报告两侧由同一个受
支持三元组集合渲染。测试中的独立穷举 oracle（回溯枚举所有匹配、直接按契约推导
最优值与完整支持集）在大量随机小文档（含 fold 双候选）上与该残差/SCC 刻画逐一
对照。

### 边界与操作

* 至多 32 条规则、32 条记录、归一前后各至多 32 个计划实例；8 MB 输入上限；
  重复 JSON 键、非有限数值、未定义字段均拒绝。
* 成功时报告原子写入 `--output` 并向 stdout 输出相同 JSON；输入/计算/文件错误
  退出 2 且不覆盖已有报告。输入与输出路径必须不同，输出符号链接拒绝。
* 重跑幂等；仅用 Python 标准库，IANA 时区数据取自操作系统。

```sh
.venv/bin/calendar-reconcile --source fixtures/reconcile.json --output run/reconcile.json
```

## 持久化日历提醒队列 v1（`calendar-reminders`）

第四个、增量式入口的完整数据契约（名义日期与实际开始时间、payload 相等性、
严格 JSON、单调时钟、快照替换与活跃领取保护、claim/ack 重试与过期语义、
SQLite 事务边界与所选持久投影的正确性义务、联动手算样例）见独立文档
[contract_reminders.md](contract_reminders.md)。前三个入口的接口与报告语义不变。

## 员工每日会议负载统计 v1（`calendar-workload`）

`calendar-workload` 是第五个、增量式入口。前四个入口的接口与报告结构保持不变。
它统计每名员工在指定本地日期范围内每一天被会议占用的时间，按真实 UTC
秒数计量，并对超过每日上限的日期打标。

### 输入

JSON 根恰含 `from_date`、`through_date`、`employees`、`schedules`。

* `from_date`、`through_date` 是严格的 `YYYY-MM-DD` 有效日历日期，范围含首尾
  两天，且含首尾最多 3660 天（即 `through_date - from_date <= 3659` 天）。
* `employees` 是数组，至多 32 项；每项恰含 `employee`、`timezone`、
  `daily_limit_minutes`。`employee` 遵循 `common.ident` 且全局唯一；
  `timezone` 必须是系统可用的 IANA 时区名；`daily_limit_minutes` 是
  0..1440 的整数（布尔值不是整数）。
* `schedules` 是数组，至多 32 项；每项恰含 `employee`、`rule`。`employee`
  必须引用已声明的员工；`rule` 是 `model.plan` 使用的完整日历规则对象
  （字段与全部边界沿用展开模型），其 `schedule` 在全部规则间全局唯一。
* 全部规则展开后的实例总数至多 8192；展开为空的规则合法。`employees` 与
  `schedules` 都允许为空数组。
* 所有输入先完整验证：任何空展开、超限日或其他早期业务结论都不会跳过对
  末尾非法员工、非法引用或非法规则的拒绝。

### 统计语义

每个展开实例是 UTC 半开区间 `[start, end)`，`end` 按真实 elapsed 时长计算，
秒精度与历史秒级 UTC 偏移（如巴黎 1900 年的 `+00:09:21`）原样保留。规则
时区决定重复日历在何时发生；员工时区独立决定占用归入哪一个本地日期。

员工某本地日的 UTC 原像是 `[day_start(d), day_start(d+1))`，其中
`day_start` 是当地午夜对应的真实 UTC 时刻：

* 正常午夜唯一对应一个 UTC 时刻；
* 午夜落在回拨（fold）区间时取最早的有效时刻（例如智利秋季 24:00 回拨日
  的 25 小时长度由此保留）；
* 午夜被跳过时（智利春季 24:00 跳时、Apia 2011-12-30 整日跳过），解析为
  缺口之后第一个有效墙上时刻，即缺口结束瞬间；被整日跳过的日期原像为空，
  占用为 0 秒。

因此每个本地日的长度由时区规则决定（纽约跳时日 82800 秒、回拨日 90000 秒、
巴黎 1911-03-10 为 86961 秒），绝不假设固定的 86400 秒。

`busy_seconds` 是该员工全部实例区间裁切到该本地日原像后的**并集**长度：
同一员工的多条日历或实例在 UTC 上重叠时不重复计时，端点相接
（`a.end == b.start`）不重叠；不同员工各自独立统计。跨员工本地午夜的区间
把午夜前后的真实秒数分别归入相邻日期，一个实例不会因此复制成两个日历实例。
`busy_seconds` 严格大于 `daily_limit_minutes × 60` 时 `over_limit=true`，
等于上限不超限；无占用日期仍输出 `busy_seconds=0`、`over_limit=false`。
范围外日期上的占用不计入范围内日期（跨范围边界的区间只计入落在范围内
日期上的部分）。

### 结果外壳与排序

沿用 `schema_version=1`、`source_sha256`（原始输入字节的 SHA-256）与
`result` 外壳。`result` 恰含 `from_date`、`through_date`、`employees`。
`employees` 按员工标识的 Unicode 码点升序；每项恰含 `employee`、
`timezone`、`daily_limit_minutes`、`days`。`days` 对范围内每个本地日期
恰有一项、按日期升序，每项恰含 `date`、`busy_seconds`、`over_limit`。
空员工列表产生空 `employees`；有员工但无日历时所有范围日期
`busy_seconds=0`。

### 算法

按员工把全部实例区间合并成排序的不相交并集（保留半开端点与秒精度），
再把该并集与按升序排列的每个本地日 UTC 原像做一次前向扫描求交。每个被
覆盖的秒恰好归入一个本地日期；合并与扫描都不依赖"每日 86400 秒"的假设。
另一种"先按本地日切分各区间、再在员工与日期内合并"的设计同样正确，本
实现选择先合并后按日求交，其正确性义务集中在并集合并的半开语义、日界
原像的时区规则来源与扫描的完整分区。

### 手算样例（`fixtures/workload.json`）

纽约员工 alice（每日上限 100 分钟）在 2024-03-10 春季跳时日有两条日历：
01:30 开始的 120 分钟（01:30 EST = 06:30 UTC，区间 `[06:30, 08:30)`）与
02:30 开始的 60 分钟（02:30 不存在，`shift_forward` 平移到 03:00 EDT =
07:00 UTC，区间 `[07:00, 08:00)`）。两条 UTC 区间重叠，并集为
`[06:30, 08:30)` = 7200 秒，而非逐条求和的 10800 秒。7200 > 100×60，
该日 `over_limit=true`。

### 边界与操作

* 至多 32 名员工、32 条规则、8192 个展开实例；日期范围含首尾至多 3660
  天；8 MB 输入上限；重复 JSON 键、非有限数值、未定义字段均拒绝。
* 成功时报告原子写入 `--output` 并向 stdout 输出相同 JSON；输入/计算/
  文件错误退出 2 且不替换已有报告。输入与输出路径必须不同，输出符号链接
  拒绝。
* 重跑幂等；仅用 Python 标准库，IANA 时区数据取自操作系统。

```sh
.venv/bin/calendar-workload --source fixtures/workload.json --output run/workload.json
```

## 收件方专属日历视图与差量更新 v1（`calendar-feed`）

第六个、增量式入口的完整数据契约（发布源与快照校验、按最高授权的可见性、
公开键与内容指纹、差量形状与接收端应用顺序、验收样例）见独立文档
[contract_feed.md](contract_feed.md)。前五个入口的接口与报告语义不变。
