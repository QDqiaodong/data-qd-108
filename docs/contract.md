# 寿命与删失口径

输入 CSV 列序：asset_id（唯一设备）、cohort（部件批型）、installed（安装日）、
failed（首次故障日，可空）。全部是合成设备，不用于真实安全或保修结论。
日期严格 YYYY-MM-DD，按自然日差计寿命。每台设备仅一个安装-首次故障周期。
failed 早于 installed、重复设备和空必填字段拒绝。后于 as_of 的安装排除并列明；
观测截止前及当天的故障算事件，未来故障或空故障右删失于 as_of。

KM 在相同寿命日先按完整风险集计算故障概率，再减去该日故障和删失。
风险集按寿命轴而非日历轴，不处理左截断。允许零日故障。生存率精确分数计算，
输出同时含 fraction 与浮点近似。中位寿命为首次 S(t)<=0.5 的日；未达到为 null，
不能用最大随访日替代。曲线按右连续阶梯定义。
RMST 为从0到统一 horizon 的生存曲线面积，单位设备日；horizon 正整数且不超过
每个队列的最大随访，避免对观察期外外推。它不是已失败设备寿命的算术平均值。

JSON 包含纳入个体、排除清单、每组风险表/事件/删失/生存曲线和 RMST。
全量确定性计算，验证后原子替换单个 JSON；失败不改已有报告。
不提供置信区间、显著性检验、因果解释、复发事件、协变量或迟到数据重算。

# KM 不确定性报告口径（独立入口，schema_version=1）

入口 `python -m repair_survival.uncertainty --source CSV --as-of 日
--horizon N --confidence-level C --output JSON`；对应 Python 调用
`repair_survival.uncertainty.analyze(source, as_of, horizon, output,
confidence_level=0.95)`，另有只构建报告 dict 的 `build_report(...)`。
输入仍为精确四列表头 asset_id,cohort,installed,failed，沿用严格日期、
重复身份、截止日与未来安装排除规则；每份源文件只读取一次原始字节，
解析与 SHA256 来自同一份字节。不改变 subjects/estimate/report/cli 的
行为、返回值与原有输出字段（原报告无 schema_version 字段，保持不变）。

## 参数与置信水平

horizon 为正整数日（布尔值不算整数）。confidence-level 为十进制数，
接受 [0.80, 0.99] 含边界，默认 0.95；NaN、Infinity、布尔值、字符串及
范围外值拒绝。双侧标准正态分位数由标准库 math.erf 二分求逆实现，
不硬编码特定置信水平常数，精度优于 1e-12。

## 逐节点 Greenwood 与 log(-log S) 区间

事件年龄 t_j 取事件前完整风险人数 Y_j 与同龄事件数 d_j；并列故障作为
同龄 d_j 处理，删失共享事件前 Y_j 但不进入分子。Greenwood 累计项
G(t)=Σ d_j/[Y_j(Y_j-d_j)]，生存率与风险集成员沿用既有 KM 口径。
当 0<Ŝ(t)<1 且 G(t) 有限，采用 W=log(-log Ŝ) 的双侧正态区间
W ± z·sqrt(G)，反向变换 exp(-exp(·))（单调递减，注意上下界交换），
裁剪到 [0,1]，边界严格有序；不得用正态区间直接裁剪替代。退化情形：
Ŝ=1 且 G=0 时区间 [1,1]（method=degenerate_one）；Ŝ=0 时区间
[0,0]（method=degenerate_zero）。Y_j=d_j 使后续 G 不再有限：该节点
起 greenwood_cumulative 为 null，Ŝ=0 的区间仍给 [0,0]。所有区间均为
逐点置信区间（interval_type=pointwise），不是全曲线同时置信带。

## RMST 方差与区间

RMST 点值为既有右连续 KM 曲线 0 到 horizon 的面积，口径与原逻辑一致。
对每个事件时点 t_j<horizon，令 A_j 为 t_j 到 horizon 的 KM 曲线面积，
Var(RMST)=Σ A_j²·d_j/[Y_j(Y_j-d_j)]；事件恰在 horizon 时 A_j=0，
不改变方差。区间为 RMST ± z·SE 的正态近似，截断到 [0,horizon]。
若任一正面积项发生 Y_j=d_j，方差不可由该公式有限估计：RMST 点值仍
输出，variance/standard_error/interval 为 null，
status=reason=greenwood_singular，不跳过事件也不误报零方差。观测支持
缺口（risk_set_gap / no_baseline_support）使 horizon RMST 不可估计时，
rmst_days 与区间同为 null 并沿用对应 reason；这是正常诊断，命令成功。
零日事件按原项目约定处理并接受同样奇异判断。

## 输出结构、序列化与失败约定

报告含 schema_version=1、mode=kaplan_meier_uncertainty、parameters
（as_of、horizon_days、confidence_level、source_sha256）、排除清单、
interpretation（逐点而非同时带、变换公式、无因果解释、仅合成分析）与
cohorts。每 cohort 给 status、support_end_day、gap、与旧 estimate 完全
一致的 km 点估计块、覆盖 0/horizon 及全部事件删失边界的 risk_table
（逐节点列年龄、entered/at_risk/failures/censored 成员与计数、
at_risk_after、事件后 KM 点值 fraction+value、Greenwood 单项与累计、
区间端点与 method），以及 horizon 摘要（RMST 点值、方差、标准误、
区间端点、status、reason）。精确量以 Fraction 递推并输出规范约分
fraction 与浮点 value；浮点不参与计算，allow_nan=False 保证不出现
NaN/Infinity。无当前时间、绝对路径等随运行变化字段，同参数字节一致。
先完成校验、分析与序列化，再经输出同目录临时文件原子替换；缺失源、
坏日期、非法置信水平、契约失败、写入或替换失败返回非零并清理临时
文件，已有报告保持完整，修正后可直接重跑。

# 延迟进入竞争风险口径（schema_version=2）

入口 `python3 -m repair_survival.competing --source CSV --as-of 日 --horizon N --output JSON`；
对应 Python 调用 `repair_survival.competing.analyze(source, as_of, horizon, output)`。
估计的是现有预防性更换策略下，指定使用年龄前失效与更换两种竞争退出的
累计概率及限制平均在役时间；不是取消更换后的因果失效风险。

## 输入契约

UTF-8 CSV，有序表头精确为 asset_id,cohort,installed,observed_from,
exit_date,exit_kind。asset_id 全文件唯一；asset_id 与 cohort 非空且无
首尾空白。日期严格 YYYY-MM-DD。observed_from 为开始监测当天密封件仍在
使用，不得早于 installed。exit_date 与 exit_kind 同时为空或同时非空；
exit_kind 仅 failure（首次失效）或 replacement（失效前更换）。显式退出
不得早于 observed_from，允许同日进入与退出。每台设备仅一个原密封件
周期，更换后不建立第二周期。重复设备、未知类型、缺列、多列、参差记录、
非法日期关系均整次拒绝，包括后来会因截止日被排除的行。

## 截止日转换

as_of 包含当天。installed 晚于截止日排除为 not_installed_by_cutoff；
其余 observed_from 晚于截止日排除为 not_enrolled_by_cutoff。已纳入设备
声明退出在截止日或以前时按其类型；退出在未来或未声明时行政右删失于
截止日，censor_reason 分别为 exit_after_cutoff / no_declared_exit，
声明日期与类型保留在 declared_exit_* 字段。全部设备被排除时整次失败。
分析时间轴始终是距安装日的自然日年龄：进入年龄 e=observed_from-installed，
有效退出年龄 x=有效退出日-installed，不从监测日重计寿命。horizon 为正
整数年龄日；Python 调用中布尔值不算整数。

## 估计算法

每 cohort 独立。年龄 t 先加入 e=t 的设备，再取风险集 Y(t)={e<=t<=x}；
当日失效、更换、删失共享退出前完整风险集 n。两类事件使用同一个事件前
在役概率 S_prev：F_failure += S_prev*d_f/n，F_replacement += S_prev*d_r/n，
S *= 1-(d_f+d_r)/n，随后移除当日退出者。删失不增加任何累计概率。初始
S=1、两类 F 为 0，零日事件同样处理。凡估计有定义时 S+F_failure+
F_replacement 精确等于 1。不做两个 KM 取补，也不把另一原因当普通删失。
在役 RMST 为 0 到 horizon 的右连续 S 阶梯面积（日）；horizon 当天事件
计入端点概率但不产生额外正长度面积；horizon 不是新的观测截止日，不
伪造当天行政删失。算法只扫描真实年龄边界，不逐日展开。

## 观测支持

检查 0、进入、退出年龄与 horizon 形成的相邻边界区间。边界完成当日退出
后，若某正长度区间内无在风险设备且当前 S>0，则该 cohort 自该区间起不
可估计：记录首个空档起止年龄与 support_end_day，保留此前可信概率前缀；
后续再进入不能恢复、不重新归一化、不跨空档连接。无年龄零支持标记
no_baseline_support，前缀后失去支持标记 risk_set_gap。horizon 越过缺口
时三项概率与完整 RMST 为 null（不是 0）；这是数据支持不足，CLI 仍成功
输出诊断，其他 cohort 照常分析。S 精确归 0 后零在役尾部不算缺口，可
积分到 horizon；最后删失恰在 horizon 时此前区间仍有支持，不算失败。

## 输出结构（schema_version=2，mode=delayed_entry_competing_risks）

- parameters：as_of、horizon_days、source_sha256（输入原始字节，只捕获一次）。
- devices：全部设备的 record（从 1 计，表头不计）、声明退出、转换结果
  （entry_age_days、exit_age_days、exit_kind、censor_reason）或排除原因。
- cohorts[name]：status、subjects、support_end_day、gap、prefix（截至
  support_end_day 的可信概率与 RMST）、horizon（端点三项概率与 RMST，
  不可估计时为 null）、risk_table。
- risk_table 覆盖 0、horizon 及区间内全部进入、退出边界；节点列出
  entered、at_risk（完整风险集 ID 与计数）、failures/replacements/
  censored（ID 与计数）、at_risk_after 及三项概率；失去支持后的节点
  仍给原始成员与计数，概率字段为 null 且不继续累积。
- 每个有效概率及 RMST 同时输出规范约分数字符串 fraction 与有限浮点
  展示值 value；value 不参与计算，中间递推不经过 float。

## 假设与限制

行政删失与延迟进入需与两种退出风险独立（非信息性删失/进入）；纳入监测
前未观测到的退出不进入本分析。更换概率依赖现行维护策略，策略改变后的
失效风险不可由本报告直接推出。不提供置信区间、显著性检验或真实安全、
保修结论。同一输入字节与参数产生字节一致 JSON；先完成校验、计算、
序列化，再经同目录临时文件原子替换；失败保留旧报告，修正后可直接重跑。

# 离线工作簿采集口径（workbook_collection，schema_version=1）

入口 `python -m repair_survival.workbook --source FILE.xlsx --output-dir DIR`；
对应 Python 调用 `repair_survival.workbook.collect(source, output_dir)`，
另有只构建审计 dict 的 `build_collection(file_bytes)` 与 `load_collection(source)`。
本口径只做上游采集与关联校验：不计算生存曲线、竞争风险、RMST 或置信区间，
不接受 as-of 参数，也不因“未来”日期过滤台账。源文件字节只读取一次，
SHA256 与全部解析都基于这份字节，之后不再从源路径打开文件。

## 支持范围与包定位

- 仅支持文档化的普通 OOXML `.xlsx`（zip 容器）；拒绝 `.xls`、非 zip 的加密
  文件、损坏包、缺失必需部件、含 DOCTYPE 的 XML 与宏启用工作簿；不执行宏、
  公式或任何计算。
- 通过 `xl/workbook.xml` 与其关系文件定位名称**恰为** `Assets`、`Faults` 的两个
  worksheet，不假定 sheet1.xml/sheet2.xml，不按工作表顺序猜角色；允许其他
  工作表（审计标记 role=ignored，不参与采集）。引用部件必须包内实际存在且
  唯一，内容类型必须是 worksheet；外部关系（TargetMode=External）不跟随。
- 日期纪元按 `workbookPr.date1904` 选择，缺失按 1900 制。

## 表头、数据行与真实坐标

- 两张表第 1 行是表头，按表头文本匹配列，不按固定列号，列序可交换。
  Assets 必需 `asset_id,cohort,installed`；Faults 必需
  `ticket_id,asset_id,failed_on,status`。允许额外列；每个非空表头必须唯一，
  必需表头缺失或重复整次拒绝。
- 只处理任一必需字段有内容的数据行；必需字段全空的行忽略，只有样式、批注或
  额外列内容的行不创造记录。样式表尾行与 worksheet `dimension` 不能代替真实
  单元格扫描；隐藏行、筛选隐藏行仍是事实，照常采集。Assets 至少一条，
  Faults 允许没有数据行。
- 单元格按真实 `r` 坐标定位：稀疏缺失不把后列左移；拒绝同一工作表重复坐标、
  坐标与所在 `row r` 不一致、以及实际使用区域内覆盖必需列的合并单元格。
- 支持 sharedStrings 与 inlineStr 的普通文本和富文本；富文本按正文文本段
  顺序连接并保留原文，`rPh` 注音节点与 XML 缩进不混入。字符串池索引、
  worksheet 部件位置与单元格地址在审计中保持关联。不用显示格式猜业务身份。

## 字段类型与身份

- `asset_id,ticket_id,cohort,status` 只接受文本单元格，非空且无首尾空白，
  大小写敏感。文本 `0007` 与 `7` 是不同设备；数值单元格 7 即使带 `0000`
  数字格式也拒绝。`status` 精确为 `CONFIRMED` 或 `CANCELLED`。
- 必需字段出现公式一律拒绝，即使有看似合法的缓存值；布尔、错误单元格及
  不支持类型拒绝。未映射的额外列可以含公式，不计算也不影响合法采集。
- `installed,failed_on` 接受：严格 `YYYY-MM-DD` 文本；严格 OOXML 日期单元格；
  或数值单元格中的整数 Excel 序列。数值字符串可含小数点但必须精确为整数，
  不经 float、不四舍五入；分数日、指数、正号等写法拒绝。日期有效性不依据
  单元格 numFmt。

## 日期序列规则

- 1900 制：序列 1 = 1900-01-01，59 = 1900-02-28；60 是 Excel 虚构的
  1900-02-29，必须拒绝；61 = 1900-03-01。拒绝负序列与 0。
- 1904 制：序列 0 = 1904-01-01；拒绝负序列。两种日期制都拒绝超出
  `datetime.date` 范围的值。

## 关联校验与最早故障选择

- Assets 中 `asset_id` 唯一；Faults 中 `ticket_id` 全表唯一，同 ID 重复即使
  内容相同也拒绝。每张工单必须引用已登记设备；`failed_on` 不得早于该设备
  `installed`；CANCELLED 记录同样先完成这些校验。
- 对每台设备，只从 CONFIRMED 工单选择最早 `failed_on`；日期并列时按
  `ticket_id` 原文 Unicode 码点顺序选一张作为规范依据。其他确认工单与取消
  工单保留在审计中并记录未选中原因
  （selected_earliest_confirmed / confirmed_same_date_lost_ticket_id_order /
  confirmed_later_than_selected / cancelled_not_eligible）。
- 没有确认工单的设备输出空 `failed`，不伪造截止日。清册只描述每台设备原
  密封件周期，不从工单推断更换或第二周期，不把预防性更换混成普通删失。

## 输出、发布与失败约定

- 输出目录原先必须不存在，已有目录拒绝覆盖。两份产物先写入目标同目录的
  私有暂存目录，全部成功后一次 `os.replace` 原子发布为目录；输入/OOXML/
  字段/关联错误或写入失败均非零退出并清理本次暂存，不发布只有 CSV 或只有
  审计的半套结果，修正后可直接重跑。
- `assets.csv`：精确有序表头 `asset_id,cohort,installed,failed`，按
  `asset_id` 稳定排序，日期规范 ISO，UTF-8 无 BOM，标准 CSV 转义，可直接
  交给现有 subjects 与旧 CLI，消费者无需改动。
- `cells.json`（schema_version=1，mode=workbook_collection）：记录工作簿
  SHA256 与字节数、日期制、workbook 部件与全部工作表角色、字符串池规模、
  两张表的表头映射（含真实坐标、类型与共享串索引）、全部采集设备及工单
  （按字段保留 sheet 名、真实坐标、原始类型/文本或数字令牌、规范值）、每台
  设备的候选确认工单、选中 ticket_id 与逐票处置理由。缺失字段保留预期坐标
  且 raw=null，不移配相邻单元格。该文件足以核对转写与最早故障选择，但不
  宣称保存了原工作簿全文或证明台账真实。
- 不含当前时间、机器绝对路径、耗时等随运行变化字段；同一工作簿重跑到两个
  新目录，两份输出分别字节一致。改变列序、行序、字符串池或工作表部件排列
  不改变 CSV 业务内容，审计坐标与原文摘要如实变化。
