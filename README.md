# data-qd-163 · 带时区的重复日历展开与共享资源准入

Bounded recurring local schedules across daylight-saving transitions，以及在共享资源容量下按整条重复日历做精确准入。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

本仓有两个成功时都发布报告的命令：

- `calendar-expand --source INPUT --output REPORT`：把单条重复日历展开成 UTC 实例（原有行为，报告结构不变）。
- `calendar-admit --source INPUT --output REPORT`：把一组重复日历申请整组接受或拒绝，在容量可行前提下选出总优先级精确最大的方案。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；建议 Python 3.12。使用 uv 建立独立环境，构建工具锁版本。首次安装无公共缓存时需要访问包索引；业务运行不访问网络。

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/calendar-expand --source fixtures/input.json --output run/report.json
.venv/bin/calendar-admit --source fixtures/admission.json --output run/admission.json
.venv/bin/python -m unittest discover -s tests -v
```

修改源码后重新安装。展开样例连续周次，并报告 `2024-03-10` 的本地时间缺口被跳过；秋季重复时间的 earlier/later 策略由测试覆盖。准入样例的 `triage + scopy` 为最优集，`swallow` 与 `motility` 被拒并各带一个容量冲突见证。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
准入的业务无解（mandatory 自身超容）属于成功计算：仍写报告、退出 0。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

## 准入语义与算法

申请是整条重复日历，必须整组接受或拒绝。占用按展开后的 UTC 半开区间 `[start, end)` 与 elapsed 时长计；同一申请内重叠实例按实例数累加，不做并集；端点相接不共享时刻。容量必须在该资源上每个有占用的 UTC 基本区段同时成立。目标是总优先级精确最大（mandatory 必选且每组只计一次）；并列时接受集合按 Unicode 码点排序后的列表字典序最小，较短共同前缀优先。

实现先用全部实例端点把每个资源切成基本区段；签名相同（同一组“申请、有效 units”覆盖）的区段对任何子集施加相同约束，于是被合并成容量等价类，求解因此只处理类而不是时间点。每类长度保留，供单资源“单位分钟”松弛使用。可选组再按共享类做并查集分解成互不相交的分量，在分量内用分支定界精确求解；上界取多个合法单约束分数背包松弛的最小值，枚举在界内穷尽，故是全局最优而非逐组贪心或仅成对冲突。相同优先级且类操作完全相同的申请按可互换组处理，保持最优值与平局规则不变。

平局时按 Unicode 码点排序后的接受 ID 列表字典序最小（较短共同前缀优先）。每个分量先按“优先级/单位分钟”启发式顺序跑一遍求值得到强下界，再按 ID 顺序、目标分数精确重跑一遍；相同分数的集合互不为子集，所以第一遍达到该分数的集合即所需列表。拒绝见证不取全量申请网格，而是用最终接受集合加该组实际实例重新划分基本区段，取最早（`start_utc` 最小，同刻 `resource` 最小）的超容正长度区段；`load` 保留实例重数，`active_ids` 只列出该段真实有占用的组。mandatory 冲突用同一重算方式仅取 mandatory 组。数据契约中说明了所选结构与另一种“逐候选重算区间”设计的正确性维护代价对比。

## 源代码布局

src/calendar_expand/model.py 为展开业务模型，admit.py 为准入校验、精确求解与见证，
common.py 提供受限JSON及报告发布，cli.py 与 admit_cli.py 分别衔接两个命令。
fixtures 为明确自制样例，代码与样例采用MIT许可。

时区通过标准库 zoneinfo 读取运行系统的 IANA 时区数据库；系统必须提供目标时区（演示使用 America/New_York）。无数据库的系统需先准备 tzdata，本仓未内置该数据库。
