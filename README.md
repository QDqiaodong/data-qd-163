# data-qd-163 · 带时区的重复日历展开、共享资源准入与计划执行核销

Bounded recurring local schedules across daylight-saving transitions，以及在共享资源容量下按整条重复日历做精确准入、把计划实例与实际执行记录做同等最优核销。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

本仓有三个成功时都发布报告的命令：

- `calendar-expand --source INPUT --output REPORT`：把单条重复日历展开成 UTC 实例（原有行为，报告结构不变）。
- `calendar-admit --source INPUT --output REPORT`：把一组重复日历申请整组接受或拒绝，在容量可行前提下选出总优先级精确最大的方案。
- `calendar-reconcile --source INPUT --output REPORT`：核销计划日历实例与实际执行记录，报告所有同等最优解释下仍可成立的三元组归属、缺失与未配对可能性，而不挑一个任意配对当作确定事实。

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
.venv/bin/python -m unittest discover -s tests -v
```

修改源码后重新安装。展开样例连续周次，并报告 `2024-03-10` 的本地时间缺口被跳过；秋季重复时间的 earlier/later 策略由测试覆盖。准入样例的 `triage + scopy` 为最优集，`swallow` 与 `motility` 被拒并各带一个容量冲突见证。核销样例用纽约 2024-11-03 回拨记录演示：第二条 UTC 记录参与竞争时，回拨记录被最大配对固定到 later 实例；移除该记录后两个 fold 候选都保留为支持。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

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

## 源代码布局

src/calendar_expand/model.py 为展开业务模型，admit.py 为准入校验、精确求解与见证，
reconcile.py 为核销校验、最小费用流与零残差 SCC 支持判定，
common.py 提供受限JSON及报告发布，cli.py、admit_cli.py 与 reconcile_cli.py 分别衔接三个命令。
fixtures 为明确自制样例，代码与样例采用MIT许可。

时区通过标准库 zoneinfo 读取运行系统的 IANA 时区数据库；系统必须提供目标时区（演示使用 America/New_York）。无数据库的系统需先准备 tzdata，本仓未内置该数据库。
