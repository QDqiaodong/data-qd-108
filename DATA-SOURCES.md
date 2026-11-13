# 数据来源与许可

本仓库 fixture 由本项目自行编写，全部是确定性合成数据；实体、编号、日期、
业务事件均不对应真实客户、机构、生产或运行历史。fixtures/assets.csv 为
安装-首次故障寿命样例，fixtures/competing.csv 为延迟进入＋失效/更换
竞争退出样例，fixtures/uncertainty.csv 为 KM 逐点不确定性与 RMST 方差
样例（含顺序失效、同龄并列故障、无故障与未来安装排除），fixtures/deid.csv
与 fixtures/deid_tree.json 为脱敏建仓与发布样例（含删失、未来安装排除与
需上泛的稀有组）。样例与原创代码按本仓库 MIT 许可分发。未复制第三方代码或
数据，不需要真实账号、网络接口或外部服务。Python 标准库由其自身许可
约束，本仓库不重新授权 Python。

`scripts/generate_workbook_fixtures.py` 与 `repair_survival.workbook.synth`
生成的 .xlsx 台账同样是确定性原创合成数据（实体编号、序列日期、工单均为
虚构），用于离线采集适配器的可重复测试；仓库不包含、也不接受提交真实设备
台账、运行产物或凭据。
