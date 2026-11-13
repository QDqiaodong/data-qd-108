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

- `src/repair_survival/`：解析、业务转换及命令行入口（含竞争退出口径 competing）。
- `fixtures/`：MIT 许可的合成输入（assets.csv 寿命删失、competing.csv 竞争退出）；来源声明见 DATA-SOURCES.md。
- `docs/contract.md`：字段和口径、边界及失败约定。
- `tests/`：业务结果、边界与失败测试。
- `requirements*.lock`、`pyproject.toml`：环境和构建声明。

本基线不承诺实时同步或生产调度。仅验证本机环境的事实见交付记录；本仓库不
 声称已经验证任何特定 Linux 容器。所有路径均由调用参数提供，无宿主机绝对路径。
