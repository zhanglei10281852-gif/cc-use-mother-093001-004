"""净空、隔离距离与工序先后的业务规则。

规则均为显式数据，便于运营单位按规范调整；冲突原因中引用这些数值。
"""

UTILITY_LABELS = {
    "POWER": "电力",
    "TELECOM": "通信",
    "WATER": "给水",
    "FIRE": "消防",
    "GENERAL": "通用",
}

PROCESS_LABELS = {
    "STRUCTURE_REPAIR": "结构修补",
    "PIPE_LAYING": "管线敷设",
    "CABLE_LAYING": "线缆敷设",
    "PRESSURE_TEST": "压力试验",
    "RESTORE": "回填恢复",
    "MAINTENANCE": "巡检维护",
    "EMERGENCY": "抢修",
    "GENERAL": "一般作业",
}

# 不同专业作业包络之间的最小隔离距离（米）；未列出的组合回落到区段默认值。
SEPARATION_TABLE_M = {
    frozenset({"POWER", "TELECOM"}): 0.5,
    frozenset({"POWER", "WATER"}): 0.5,
    frozenset({"POWER", "FIRE"}): 0.5,
    frozenset({"POWER", "GENERAL"}): 0.5,
    frozenset({"WATER", "TELECOM"}): 0.3,
    frozenset({"FIRE", "TELECOM"}): 0.3,
    frozenset({"WATER", "FIRE"}): 0.3,
}

# 前后工序：(先序工序, 后续工序)。空间上同属一个作业面时，
# 后续工序不得在先序工序占位结束前开始。
PROCESS_ORDER = [
    ("STRUCTURE_REPAIR", "PIPE_LAYING"),
    ("STRUCTURE_REPAIR", "CABLE_LAYING"),
    ("PIPE_LAYING", "PRESSURE_TEST"),
    ("PIPE_LAYING", "RESTORE"),
    ("CABLE_LAYING", "RESTORE"),
    ("PRESSURE_TEST", "RESTORE"),
]


def utility_label(utility: str) -> str:
    return UTILITY_LABELS.get(utility, utility)


def process_label(process: str) -> str:
    return PROCESS_LABELS.get(process, process)


def required_separation_m(util_a: str, util_b: str, section_default_m: float) -> float:
    """两专业作业包络间的最小隔离距离；同专业按区段默认隔离执行。"""
    if util_a == util_b:
        return section_default_m
    return SEPARATION_TABLE_M.get(frozenset({util_a, util_b}), section_default_m)


def process_must_precede(earlier: str, later: str) -> bool:
    return (earlier, later) in PROCESS_ORDER
