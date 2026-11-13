# data-qd-108 · 工业密封件寿命分析

将工业设备密封件的安装日和首次故障日转为生存分析个体，避免把尚未故障的设备丢掉。
按部件批型计算 Kaplan–Meier 生存曲线、风险集、中位寿命和限制平均寿命（RMST），
并输出可逐个核验的 JSON 风险表，不依赖图形桌面。

在截止日2026-01-11、horizon=10时，seal-A 有4台、2次故障，生存率依次3/4、1/2；
5日的故障与删失共享风险集3，中位寿命5日、RMST=6.75日。seal-B 两台均删失，
RMST=10日、中位数null；未来安装F1单列排除。

## 竞争退出分析（延迟进入＋失效/更换）

维护台账中的设备可从安装后的监测日才纳入；失效前的预防性更换会终止原
密封件周期。独立入口按现有更换策略估计指定年龄前失效与更换的竞争退出
概率及限制平均在役时间，口径与JSON结构见 docs/contract.md：

```sh
.venv/bin/python -m repair_survival.competing --source fixtures/competing.csv --as-of 2026-01-07 --horizon 6 --output run/competing.json
```

在截止日2026-01-07、horizon=6时，seal-C 五台：年龄2失效A（风险集3）、
年龄4更换B（E先进入，风险集4）、年龄6失效D（C、E同日删失，风险集3）；
在役S=1/3、失效累计1/2、更换累计1/6、在役RMST=13/3≈4.33日。seal-Z
更换累计1/2、S=1/2、RMST=5日；seal-Y 无年龄零观测支持，标记
no_baseline_support，horizon 指标为 null；H（未来安装）、I（未来监测）
分列排除。对应 Python 调用为
`repair_survival.competing.analyze(source, as_of, horizon, output)`。

## KM 生存估计不确定性报告（独立入口，schema_version=1）

在不改变原寿命分析行为与输出的前提下，新增独立入口为每个 cohort 的
Kaplan–Meier 曲线和限制平均寿命（RMST）补充观测支持与逐点不确定性：
逐风险节点给事件前完整风险集 Y_j、同龄事件数 d_j、Greenwood 累计量
d_j/[Y_j(Y_j-d_j)] 与 log(-log S) 变换双侧正态逐点区间；horizon 摘要给
RMST 点值、方差 Σ A_j²·d_j/[Y_j(Y_j-d_j)]、标准误与截断到
[0,horizon] 的正态区间。所有区间为逐点区间而非全曲线同时置信带，报告
明确不提供因果解释，仅用于合成分析，不用于安全、保修或因果结论。

```sh
.venv/bin/python -m repair_survival.uncertainty --source fixtures/uncertainty.csv --as-of 2026-01-11 --horizon 10 --confidence-level 0.95 --output run/uncertainty.json
```

`--confidence-level` 接受 0.80 至 0.99（含边界）十进制数，默认 0.95，
拒绝 NaN/Infinity 与范围外值；正态分位数由标准库 `math.erf` 二分求逆，
未硬编码 95% 常数。源文件只读取一次原始字节，解析与 SHA256 同源。
无故障 cohort 方差为 0、曲线区间 [1,1]；完全失效 Ŝ=0 区间 [0,0]；
Y_j=d_j 的 Greenwood 奇异保留 RMST 点值但方差/区间为 null
（greenwood_singular）；观测支持缺口时 RMST 区间为空并标注
risk_set_gap/no_baseline_support。统计支持不足与方差奇异是成功输出的
诊断，不是输入失败。对应 Python 调用为
`repair_survival.uncertainty.analyze(source, as_of, horizon, output,
confidence_level=0.95)`；口径与字段见 docs/contract.md。

## 离线工作簿采集适配器（xlsx → 原四列输入）

维修人员提交的是 Excel 维护台账而不是四列 CSV：设备编号可能带前导零、日期
可能是 Excel 序列值、故障工单在另一张工作表。新增离线适配器把名称恰为
`Assets`、`Faults` 的两张清册采集为原有输入,并产出可逐单元格复核的审计。
它只做上游采集与关联校验,不计算或扩展生存曲线、竞争风险、RMST 或置信区间。

```sh
.venv/bin/python -m repair_survival.workbook --source fixtures-ish/ledger.xlsx --output-dir run/ledger
# run/ledger/assets.csv  精确表头 asset_id,cohort,installed,failed,可直接交给旧 CLI
# run/ledger/cells.json  SHA256、日期制、部件/表头映射、逐单元格原文令牌与选择理由
```

对应 Python 调用为 `repair_survival.workbook.collect(source, output_dir)` 与只构建
审计 dict 的 `repair_survival.workbook.build_collection(file_bytes)` /
`load_collection(source)`。支持范围与口径见 docs/contract.md「工作簿采集口径」。
合成台账由 `python3 scripts/generate_workbook_fixtures.py --output-dir DIR` 可重复
生成(原创、确定性、无真实台账);适配器与生成器仅用 Python 3.11+ 标准库,不调用
Excel/LibreOffice、不启动浏览器、不联网,不执行宏或计算公式。

## 合作研究脱敏建仓与发布（独立入口，schema_version=1）

合作研究不能直接领取密封件台账。新增两个独立离线入口：build 把四列台账、
as-of、整数 k>=2 与单根 cohort 树（叶子精确对应输入批型）固化为一个 SQLite
仓库，保存原文、来源、纳入/排除与披露决策；publish 只读一个仓库快照，另建
全新 SQLite，仅含泛化批型、安装时间桶、故障布尔值，一设备一行、重复元组
不去重。日期、身份与截止语义沿用原四列输入。

```sh
.venv/bin/python -m repair_survival.deid build --source fixtures/deid.csv --tree fixtures/deid_tree.json --as-of 2026-01-31 --k 2 --warehouse run/deid-warehouse.db
.venv/bin/python -m repair_survival.deid publish --warehouse run/deid-warehouse.db --output run/deid-release.db
```

每个 泛化批型×时间桶 组须至少 k 台且故障/未故障并存，否则整组抑制；全局
按（抑制人数, 保留行距离和=树边数+日期级别, 日期级别, 排序叶映射）字典序
最小化，树上动态规划寻优（不枚举全部切面），仓内 dp_nodes/dp_levels 保留
最优性依据。合成例中 seal-X1/seal-X2 单台不足 k=2，上泛到 seal-X 后放行：
抑制 0、距离 2、级别 0（月），发布 6 行。这只是指定披露规则，不保证匿名。
建仓、发布各自失败保留旧目标；发布确定性，源移走仍可发布。口径与仓库/发布
物结构见 docs/contract.md「脱敏建仓与发布口径」。

## 安装与运行

要求 Python 3.11 或更高；先确认 `python3 --version`。以下命令在仓库根目录执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m repair_survival.cli --source fixtures/assets.csv --as-of 2026-01-11 --horizon 10 --output run/survival.json
.venv/bin/python -m unittest discover -s tests -v
```

运行依赖与测试仅使用标准库；首次安装构建工具通常需要联网。每个仓库有独立
 `.venv`。安装的是源码的构建副本；编辑源码后须再次执行安装命令。
所有演示输出在忽略的 `run/` 中；演示可重复执行。临时目录中的测试不改变 fixture。
CLI 参数错误或无效数据返回非零；失败原因打印到 stderr。

## 目录

- `src/repair_survival/`：解析、业务转换及命令行入口（含竞争退出口径 competing、KM 不确定性口径 uncertainty、离线工作簿采集 workbook、脱敏建仓与发布 deid）。
- `scripts/`：确定性合成 xlsx 台账生成脚本（原创合成数据，可重复运行）。
- `fixtures/`：MIT 许可的合成输入（assets.csv 寿命删失、competing.csv 竞争退出、uncertainty.csv 不确定性样例、deid.csv 与 deid_tree.json 脱敏建仓样例）；来源声明见 DATA-SOURCES.md。
- `docs/contract.md`：字段和口径、边界及失败约定。
- `tests/`：业务结果、边界与失败测试。
- `requirements*.lock`、`pyproject.toml`：环境和构建声明。

本基线不承诺实时同步或生产调度。仅验证本机环境的事实见交付记录；本仓库不
 声称已经验证任何特定 Linux 容器。所有路径均由调用参数提供，无宿主机绝对路径。
