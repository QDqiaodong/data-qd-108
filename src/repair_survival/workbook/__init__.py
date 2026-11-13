"""离线工作簿采集适配器(仅标准库)。

把维修人员提交的普通 OOXML ``.xlsx`` 台账(Assets/Faults 两张清册)采集为
原有寿命分析器要求的四列 ``assets.csv``,并产出可逐单元格复核的
``cells.json``。本包只做上游采集与关联校验,不计算生存曲线、竞争风险、
RMST 或置信区间,也不执行宏或公式。

公开入口:

- :func:`collect`/:func:`load_collection`:Python 调用。
- ``python -m repair_survival.workbook --source FILE.xlsx --output-dir DIR``:独立命令。
- :mod:`repair_survival.workbook.synth`:仅用于测试与脚本的确定性 xlsx 合成器。
"""
from .adapter import WorkbookError, collect, load_collection, build_collection

__all__ = ['WorkbookError', 'collect', 'load_collection', 'build_collection']
