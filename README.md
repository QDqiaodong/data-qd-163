# data-qd-163 · 带时区的重复日历展开、共享资源准入与计划执行核销

Bounded recurring local schedules across daylight-saving transitions，以及在共享资源容量下按整条重复日历做精确准入、把计划实例与实际执行记录做同等最优核销。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

本仓有七个成功时输出结果的命令；前三个和第五个发布单次报告，第四个维护持久化工作队列，第六个维护收件方保存的视图文件，第七个维护按预约结账的共享设施账簿：

- `calendar-expand --source INPUT --output REPORT`：把单条重复日历展开成 UTC 实例（原有行为，报告结构不变）。
- `calendar-admit --source INPUT --output REPORT`：把一组重复日历申请整组接受或拒绝，在容量可行前提下选出总优先级精确最大的方案。
- `calendar-reconcile --source INPUT --output REPORT`：核销计划日历实例与实际执行记录，报告所有同等最优解释下仍可成立的三元组归属、缺失与未配对可能性，而不挑一个任意配对当作确定事实。
- `calendar-reminders --db DB --source INPUT`：在持久 SQLite 文件中维护本地日历提醒工作队列，由 `sync`/`claim`/`ack`/`inspect` 四个动作驱动同一条提醒的领取与完成生命周期；只向 stdout 输出，没有 `--output`，不接邮件、不联网、无守护进程、不读系统时钟。
- `calendar-workload --source INPUT --output REPORT`：统计每名员工在指定本地日期范围内每天的会议占用秒数与超限标记，同一员工多条日历重叠时按真实 UTC 区间并集计时。
- `calendar-feed publish --source INPUT --output FILE` 与 `calendar-feed apply --view VIEW --delta PATCH`：为外部合作方发布专属日历视图（全量或差量），接收方在没有内部源的环境下用差量原子更新自己保存的视图。
- `calendar-billing --db DB --source INPUT`：在持久 SQLite 账簿里按当前预约清单出账，免费额度按真实起点先到先用、超额计费；历史预约的重叠改动以下一张新账单的补差入账，旧账单原样归档供对账；由 `init`/`replace`/`close`/`show` 四个动作驱动，只向 stdout 输出，没有 `--output`，不读系统时钟。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；建议 Python 3.12。使用 uv 建立独立环境，构建工具锁版本。首次安装无公共缓存时需要访问包索引；业务运行不访问网络。

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/calendar-expand --source fixtures/input.json --output run/report.json
.venv/bin/calendar-admit --source fixtures/admission.json --output run/admission.json
.venv/bin/calendar-reconcile --source fixtures/reconcile.json --output run/reconcile.json
.venv/bin/calendar-workload --source fixtures/workload.json --output run/workload.json
.venv/bin/calendar-feed publish --source fixtures/feed/01_publish_full.json --output run/view.json
.venv/bin/calendar-feed publish --source fixtures/feed/02_publish_delta_busy.json --output run/delta1.json
.venv/bin/calendar-feed apply --view run/view.json --delta run/delta1.json
.venv/bin/calendar-billing --db run/ledger.db --source fixtures/billing/01_init.json
python3 -I examples/demo_reminders.py
python3 -I examples/demo_feed.py
python3 -I examples/demo_billing.py
.venv/bin/python -m unittest discover -s tests -v
```

不构建安装时，可用 `PYTHONPATH=src python3 -m calendar_expand.reminders_cli --db run/reminders.db --source fixtures/reminders/01_sync.json` 直接运行新入口，结账入口对应 `PYTHONPATH=src python3 -m calendar_expand.billing_cli --db run/ledger.db --source fixtures/billing/01_init.json`。

修改源码后重新安装。展开样例连续周次，并报告 `2024-03-10` 的本地时间缺口被跳过；秋季重复时间的 earlier/later 策略由测试覆盖。准入样例的 `triage + scopy` 为最优集，`swallow` 与 `motility` 被拒并各带一个容量冲突见证。核销样例用纽约 2024-11-03 回拨记录演示：第二条 UTC 记录参与竞争时，回拨记录被最大配对固定到 later 实例；移除该记录后两个 fold 候选都保留为支持。负载样例在纽约 2024-03-10 春季跳时日上重叠两条日历（01:30 起 120 分钟与缺口平移后的 03:00 起 60 分钟），并集 7200 秒超过每日 100 分钟上限。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
准入的业务无解（mandatory 自身超容）属于成功计算：仍写报告、退出 0。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

## 准入语义与算法

申请是整条重复日历，必须整组接受或拒绝。占用按展开后的 UTC 半开区间 `[start, end)` 与 elapsed 时长计；同一申请内重叠实例按实例数累加，不做并集；端点相接不共享时刻。容量必须在该资源上每个有占用的 UTC 基本区段同时成立。目标是总优先级精确最大（mandatory 必选且每组只计一次）；并列时接受集合按 Unicode 码点排序后的列表字典序最小，较短共同前缀优先。

实现先用全部实例端点把每个资源切成基本区段；签名相同（同一组“申请、有效 units”覆盖）的区段对任何子集施加相同约束，于是被合并成容量等价类，求解因此只处理类而不是时间点。每类长度保留，供单资源“单位分钟”松弛使用。可选组再按共享类做并查集分解成互不相交的分量，在分量内用分支定界精确求解；上界取多个合法单约束分数背包松弛的最小值，枚举在界内穷尽，故是全局最优而非逐组贪心或仅成对冲突。相同优先级且类操作完全相同的申请按可互换组处理，保持最优值与平局规则不变。

平局时按 Unicode 码点排序后的接受 ID 列表字典序最小（较短共同前缀优先）。每个分量先按“优先级/单位分钟”启发式顺序跑一遍求值得到强下界，再按 ID 顺序、目标分数精确重跑一遍；相同分数的集合互不为子集，所以第一遍达到该分数的集合即所需列表。拒绝见证不取全量申请网格，而是用最终接受集合加该组实际实例重新划分基本区段，取最早（`start_utc` 最小，同刻 `resource` 最小）的超容正长度区段；`load` 保留实例重数，`active_ids` 只列出该段真实有占用的组。mandatory 冲突用同一重算方式仅取 mandatory 组。数据契约中说明了所选结构与另一种“逐候选重算区间”设计的正确性维护代价对比。

## 核销语义与算法

实际记录的 `local_start` 是无偏移分钟级墙上时间：`fold=0` 与 `fold=1` 分别做 IANA UTC 往返验证，正常时刻得一个候选、回拨时刻得两个、缺口时刻得零个；缺口不平移也不使输入失败，而作为零候选记录保留。计划规则的 fold 策略不替实际证据预选时刻。新入口还限定分钟精度：计划实例与实际候选的 UTC 秒/微秒必须为 0，全部精度检查在身份归一与空集捷径之前完成，历史秒级偏移（如巴黎 1900 年的 `08:50:39`）整份拒绝且不覆盖旧报告。计划实例按 `(schedule, start_utc)` 归一，重复身份只核销一次。

合法三元组要求日历 ID 被允许且偏差不超过容差；每个记录与实例各至多用一次。目标先最大化配对数、再最小化绝对分钟偏差之和，时长不参与。受支持三元组是确实出现在至少一个全局同优解释中的三元组；`can_be_unmatched` 表示至少一个同优解释中该对象未配对（无合法边的对象在每个解释中都未配对，故为真）。仅一个支持且不可未配对才是确定归属，其余一律保留歧义；同一对象对的两个 fold 候选若都达最优则分别成支持，不合并。

实现把问题建成 `源→观测→实例→汇` 的单位容量最小费用最大流，弧费用为绝对偏差，带 Johnson 势的连续最短路增广精确达成两级目标。一次最优流后取 reduced cost 为零的残差图 G0：带流弧必受支持；不带流弧当且仅当自身是零 reduced cost 残差弧且两端在 G0 同一强连通分量内时受支持；观测从源可达、实例能到汇分别给出两侧的未配对可能性。最大流前提保证任何同优重路由都闭合成零残差环，因此无需枚举解释、也不会在同优解过多时截断支持（32×32 完全二分图返回完整 1024 个支持）。另一种“逐弧强制选中后重解最小费用流”的设计证明更直白但要重复 E 次最短路，取舍与正确性义务详见数据契约；测试内另有独立回溯穷举 oracle 在随机文档上交叉验证。

## 持久化提醒队列语义

提醒身份永久固定为 `(schedule, nominal_date)`：名义日期是按星期一锚点、周间隔、星期与起止日期产生、且未被 `exceptions` 排除的原始本地日期，**先保存该日期再做 gap/fold 解析**。`skip` 不生成提醒；`shift_forward` 即使跨日也不改身份；fold 改期也不改身份；不能从解析后的本地时间或 UTC 反推名义日期，也不复用 reconcile 的 `(schedule, start_utc)`。payload 恰为 `schedule`、`nominal_date`、`timezone`、`local_start`、`start_utc`、`end_utc`、`due_utc` 七字段，按全部字段相等比较；`due_utc = start_utc - lead_minutes` 个 elapsed UTC 分钟，历史秒级偏移（如巴黎 1900 年的 `08:50:39Z`）原样保留。

`sync` 完整替换规则快照（至多 32 条规则、实际生成至多 8192 条提醒，全部规则先验证）；`claim` 按 `(due_utc, schedule, nominal_date)` 领取 `due_utc <= now`、无墓碑且无有效领取的提醒，每次分配全库生命周期内不复用的新 token，租约等号即失效；`ack` 仅接受当前仍有效领取的 token，成功后写独立于快照的完成墓碑并释放领取，同 token 重试为 `duplicate=true` 且不改原 `completed_at`，其他 token 一律报错。移除后重加、改时区或改 fold 都不能复活已完成身份；无墓碑的同一身份重加可再领取但换发新 token。若新快照使任一仍有效领取的七字段 payload 消失或变化，整个 `sync` 在任何写入前被拒绝，规则、领取、墓碑与时钟保持原状。

每次成功的 `sync`/`claim`/`ack` 是同一个 `BEGIN IMMEDIATE` 事务，跨进程并发由 SQLite 写锁串行，`inspect` 只读不写且不会读到半成品；失败退出 2、不留部分业务变更，被拒绝的首次 `sync` 不留下数据库文件。完整字段契约、事务边界与持久投影的正确性义务见 [docs/contract_reminders.md](docs/contract_reminders.md)。`examples/demo_reminders.py` 用临时库演示两条联动分支，所有 `now` 显式输入、不等待租约。

## 员工每日会议负载统计语义

`calendar-workload` 是第五个、增量式入口；前四个命令的接口与报告结构不变。输入声明一组员工（各自独立的 IANA 时区与每日上限分钟）和一组完整重复日历规则，输出指定本地日期范围内每名员工每天的 `busy_seconds` 与 `over_limit`。每个展开实例是真实 elapsed 时长的 UTC 半开区间 `[start, end)`，秒精度与历史秒级偏移（如巴黎 1900 年的 `+00:09:21`）原样保留；规则时区决定会议何时发生，员工时区独立决定占用归入哪一个本地日期。

每日占用是该员工全部 UTC 区间裁切到该本地日之后的**并集**：同一员工多条日历或实例重叠不重复计时，端点相接不重叠，不同员工各自独立。本地日的 UTC 边界由时区规则决定——春季跳时日 82800 秒、秋季回拨日 90000 秒、被整条跳过的日期（如 Apia 2011-12-30）为 0 秒，绝不假设每个本地日都是 86400 秒；跨员工本地午夜的区间把午夜前后的真实秒数分别归入相邻日期。`busy_seconds` 严格大于 `daily_limit_minutes × 60` 才标记 `over_limit=true`，等于上限不超限；无占用日期仍输出 `busy_seconds=0`。实现先按员工把 UTC 区间合并成不相交并集，再与每个本地日期的 UTC 原像求交；完整输入（含全部规则）先验证，空展开或超限日不会遮住末尾的非法项。

## 收件方专属视图与差量更新语义

`calendar-feed` 是第六个、增量式入口；前五个命令的接口与报告结构不变。运营方为每个外部合作方维护一份授权快照 `{groups, activities}`：`groups` 给出该收件方在每个发布组的授权（`none`/`busy`/`detail`，等级递增），`activities` 携带完整日历规则、对外别名、标题与所属发布组。每个展开实例按活动全部所属组的最高授权决定隐藏、只显示占用时间还是同时显示标题；公开键为 `(alias, start_utc)`，时间统一 `YYYY-MM-DDTHH:MM:SSZ` 并保留历史秒偏移。内部规则 ID、组名、权限配置、隐藏活动与输入摘要都不进入对外结果，因此改动隐藏活动或占用视图看不到的标题不会改变该视图的任何字节、指纹或差量操作。

`publish` 在 `previous=null` 时输出全量视图（恰含 `schema_version,recipient,rows,view_sha256`），否则输出从 `previous` 到 `current` 的差量（恰含 `schema_version,recipient,base_sha256,target_sha256,upserts,withdrawals`）。内容指纹是把 `{schema_version,recipient,rows}` 按 `json.dumps(sort_keys=True,ensure_ascii=True,separators=(',',':'))` 序列化后对 UTF-8 字节算的 SHA-256。`apply` 先严格校验本地视图与差量形状，只接受收件方与基线指纹都匹配的差量，逐行完整替换后重算指纹，与 `target_sha256` 一致才原子写回 `VIEW`；指纹只约束内容一致性，不认证发送方，也不维护单调序号。每份快照至多 64 组、64 条规则、2048 个展开实例（无授权规则同样完整校验并计入）；视图与差量操作列表各至多 2048 项。完整契约见 [docs/contract_feed.md](docs/contract_feed.md)，可重放样例见 [fixtures/feed](fixtures/feed/README.md) 与 `examples/demo_feed.py`。

## 共享设施按预约结账语义

`calendar-billing` 是第七个、增量式入口；前六个命令的接口与语义不变。它在一本持久 SQLite 账簿中维护共享设施的预约与月度账单：`init` 固定结账时区、最早服务月与每间房的免费额度/单价；`replace` 整体替换运营方当前认可的完整预约清单；`close` 按服务月顺序出账；`show` 只读查询已关账月。成功后只向 stdout 输出该动作的结果对象（无 `schema_version` 外壳）。

每个展开实例只归一个服务月：真实 UTC 起点转换到结账时区后取当地年月，整段 elapsed 分钟都归给起点所在月，跨月也不拆分。每个 `(服务月, 房间)` 内按真实 UTC 起点先到先用免费额度，同刻按 `(schedule, nominal_date)` 码点排序；实例可部分免费，重叠不取并集，历史秒级偏移不截断。首个 `close` 只能关 `first_month`，其后逐月紧邻，跳月失败；重试已关账月原样返回归档账单，不重算补差。

新账单含两类明细：本服务月每个实例的初次计费（含免费实例），以及所有更早服务月按当前清单重算后与该行**累计已入账金额**的差额（正补收、负冲回）。行身份为 `(service_month, room, account, schedule, nominal_date)`；消失的旧行全额冲回，改房间/成本中心/服务月的实例冲旧行、记新行，绝不整笔改挂；同成本中心的正负明细不互相抵消省略。`account_totals` 只列本账单有明细的成本中心、按账号排序、允许零或负。累计入账以持久 `posted` 投影维护，故无论开过多少张补差账单比较基准都等于真实开出合计。`close` 在一个事务中同时产生账单、归档并更新累计，失败整体回滚；`show` 与重复 `close` 始终返回归档明细与汇总。完整契约见 [docs/contract_billing.md](docs/contract_billing.md)，可重放样例见 [fixtures/billing](fixtures/billing/README.md) 与 `examples/demo_billing.py`。

## 源代码布局

src/calendar_expand/model.py 为展开业务模型（`plan` 提供名义日期与解析结果分离的共享展开能力，`analyze` 在其上渲染原报告），
admit.py 为准入校验、精确求解与见证，
reconcile.py 为核销校验、最小费用流与零残差 SCC 支持判定，
reminders.py 为提醒队列的严格校验、SQLite 事务与生命周期状态机，
workload.py 为员工负载统计的严格校验、本地日 UTC 原像与区间并集按日归集，
feed.py 为收件方视图的快照校验、按授权渲染、内容指纹与差量计算/应用，
billing.py 为共享设施预约结账的服务月归属、免费额度消耗、每月账单与跨月补差，
common.py 提供受限JSON及报告发布，
cli.py、admit_cli.py、reconcile_cli.py、reminders_cli.py、workload_cli.py、feed_cli.py 与 billing_cli.py 分别衔接七个命令。
fixtures 为明确自制样例，代码与样例采用MIT许可。

时区通过标准库 zoneinfo 读取运行系统的 IANA 时区数据库；系统必须提供目标时区（演示使用 America/New_York）。无数据库的系统需先准备 tzdata，本仓未内置该数据库。
