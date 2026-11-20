# 收件方专属日历视图与差量更新 v1（`calendar-feed`）

`calendar-feed` 是第六个、增量式入口；前五个入口的接口与报告结构保持不变。
运营方为每个外部合作方发布一份**专属视图**：内部排期展开成实例后，按该收件方
在活动所属发布组里的最高授权决定每个实例隐藏、只显示占用时间，还是同时显示
标题。授权或排期调整后，运营只发布两份视图之间的**差量**；合作方在没有内部
源的环境下用差量安全更新自己保存的视图。两个子命令：

- `calendar-feed publish --source INPUT --output FILE`：从内部源发布全量视图
  （`previous` 为 `null`）或从 `previous` 到 `current` 的差量。
- `calendar-feed apply --view VIEW --delta PATCH`：校验并应用差量，原子替换
  本地保存的 `VIEW`。

运行与测试只用 Python 标准库及系统 IANA 时区数据。

## 发布源

JSON 根恰含 `recipient`、`current`、`previous`。`recipient` 是收件方标识；
`previous` 为 `null` 时输出全量视图，否则输出差量。两份快照均恰含
`groups`、`activities`：

* `groups[]` 恰含 `group`、`access`；`access` 是该收件方在该发布组的授权，
  取 `none`、`busy`、`detail` 之一，等级从低到高。
* `activities[]` 恰含 `rule`、`alias`、`title`、`groups`。`rule` 是
  `model.plan` 的完整日历规则对象（字段与全部边界沿用展开模型）；`groups`
  引用本快照发布组 ID 的数组，可为空（视为 `none`），组引用不得重复。

每份快照的 `group`、`rule.schedule` 和 `alias` 分别唯一。标识（含
`recipient`、`group`、`alias`、规则标识）和 `title` 采用 `common.ident`
的非空字符串规则，且必须是合法 Unicode 标量序列（拒绝孤立代理项码点）。
每份快照至多 64 个组、64 条规则、2048 个实际展开实例（计合并前）；允许空
快照和空展开。**无授权的规则也必须完整校验并计入上限**，末尾的非法隐藏规则
同样使整份输入失败。

## 可见性与公开行

每个展开实例的可见性取活动全部所属发布组里该收件方的最高授权：成员列表为空
视为 `none`。实例沿用原规则的时区、异常日、gap、fold 和真实经过时长，不裁切、
不合并区间、不按本地日分组。公开键为 `(alias, start_utc)`；`start_utc` 与
`end_utc` 统一为 `YYYY-MM-DDTHH:MM:SSZ`，保留历史时区的秒偏移（如巴黎 1900
年的 `08:50:39Z`）。同一规则的展开若有同键，因其时长和活动信息相同，只输出
一行。

* busy 行恰含 `alias`、`start_utc`、`end_utc`；
* detail 行多一个 `title` 字段；
* `none` 的实例不输出。

内部规则 ID、组名、权限配置、被隐藏的活动及原始输入摘要都不得进入对外结果。
因此改动隐藏活动、或仅修改占用视图看不到的标题，不能改变对应视图的任何字节、
内容指纹或差量操作；此要求适用于所有合法输入，同一收件方读到的是同一可见
内容。

## 全量视图

字段恰为 `schema_version`、`recipient`、`rows`、`view_sha256`；
`schema_version` 固定为整数 1。`rows` 按公开键的 Unicode 码点顺序排列，无
重复。内容指纹：将 `{schema_version, recipient, rows}` 按 Python
`json.dumps(sort_keys=True, ensure_ascii=True, separators=(',', ':'))`
序列化后，对 UTF-8 字节算出的 SHA-256 小写十六进制值；不含换行，也不含指纹
字段本身。

## 差量

字段恰为 `schema_version`、`recipient`、`base_sha256`、`target_sha256`、
`upserts`、`withdrawals`，版本同为整数 1。`base_sha256` 与 `target_sha256`
分别是旧、新全量视图的内容指纹。`upserts` 是新增键和同键完整可见行发生变化
的目标行（完整替换语义）；`withdrawals` 是旧有新无的公开键对象
`{alias, start_utc}`，只允许引用收件方旧视图中的键。两个列表各按公开键排列、
无重复且不能交叉。

差量必须比较授权后的完整行：失去标题权限（detail 降为 busy）时必须以无
`title` 的整行替换原行；失去最后一份可见授权时必须撤下原行。两边视图相同则
两个列表均为空且前后指纹相同。改期导致公开键改变时，撤下旧键并加入新键。

## 接收端应用

`apply` 只对收件方和基线内容指纹均匹配的差量操作，并在核对目标视图指纹后才
整体替换本地视图。处理顺序：

1. 严格校验已有全量视图并重算其内容指纹：拒绝坏指纹、未知字段、非整数 1 的
   版本、不规范时间、`end_utc` 不晚于 `start_utc`、重复或无序的键；
2. 同样严格校验差量形状和所有操作：拒绝撤下不存在的键、重复操作、同键同时
   更新和撤下，以及将完全相同的行作为更新；
3. 逐行以完整替换语义应用：先撤下、再整行替换或新增；
4. 结果按全量契约排序并重算指纹，只有与 `target_sha256` 一致才原子写回
   `VIEW`。

严格字段校验覆盖末尾非法条目：任何一步失败都不改动本地视图。指纹约束内容
一致性，不提供发送方身份认证；等同内容可再次得到相同指纹，不维护单调序号。
接收视图 `rows` 和差量的任一操作列表各至多 2048 项，应用后的 `rows` 也受此
限制。

## 边界与操作

* 所有 JSON 沿用严格读取：重复键、非有限数拒绝，单文件 8 MB 上限。
* `publish` 不得覆盖源文件（输入输出路径必须不同），`apply` 不得更新差量
  文件（视图与差量路径必须不同）；输出符号链接拒绝。
* 成功时 stdout 与文件为同一 JSON 对象；写全量视图的两条路径（`publish`
  全量与 `apply` 写回）使用相同的确定性序列化（`common.save`）。
* 非法输入、文件错误或指纹不符退出 2，只向 stderr 报错，并保留已有的输出
  或 `VIEW` 字节。重跑幂等。

## 验收样例（手算可核）

`fixtures/feed/` 三个输入演示同一活动（别名 `meeting`、UTC 单次
`2025-01-15T09:00:00Z` 至 `10:00:00Z`、标题 `内部主题`）同时属于发布组
`alpha` 与 `beta` 的授权撤销流程：

1. `alpha=detail, beta=busy`：全量视图唯一一行带 `title`，
   `view_sha256=a1f636e6…c922b9`；
2. 撤去 `alpha` 的 detail 授权而保留 `beta` 的 busy 授权：差量恰为一条无
   `title` 的完整 upsert，没有撤下；应用后视图只剩时间，
   指纹 `c970c11a…b74dcc`；
3. 再撤去 `beta` 的授权：差量只有 `meeting` 对应的撤下；应用后 `rows` 为
   空，指纹 `d285184d…259c871`。

无论源数组顺序如何，本地旧视图应用成功差量后的结果，与从相同目标源直接生成
的全量视图完全一致（逐字节相同）。对一份合法旧视图尝试错收件方、错基线或错
目标指纹的差量，三次都要失败并保留原文件；退回到同一个可见视图时按内容指纹
判断。

```sh
.venv/bin/calendar-feed publish --source fixtures/feed/01_publish_full.json --output run/view.json
.venv/bin/calendar-feed publish --source fixtures/feed/02_publish_delta_busy.json --output run/delta1.json
.venv/bin/calendar-feed apply --view run/view.json --delta run/delta1.json
```
