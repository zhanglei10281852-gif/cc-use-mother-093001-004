"""领域契约：舱室横截面、连续区段、占位包络、时间窗与施工依赖。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple, Union


def parse_dt(value: Union[datetime, str]) -> datetime:
    """接受 datetime 或 ISO8601 字符串。"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise TypeError(f"无法解析时间: {value!r}")


class UtilityKind(str, Enum):
    """入廊管线专业。"""

    POWER = "power"  # 电力
    COMM = "comm"  # 通信
    WATER = "water"  # 给水
    FIRE = "fire"  # 消防

    @property
    def label(self) -> str:
        return UTILITY_LABELS[self]


UTILITY_LABELS = {
    UtilityKind.POWER: "电力",
    UtilityKind.COMM: "通信",
    UtilityKind.WATER: "给水",
    UtilityKind.FIRE: "消防",
}

# 恢复顺序的默认优先级：数值越小越先恢复（消防/给水优先恢复供给）。
DEFAULT_RECOVERY_PRIORITY = {
    UtilityKind.FIRE: 10,
    UtilityKind.WATER: 20,
    UtilityKind.POWER: 30,
    UtilityKind.COMM: 40,
}


@dataclass(frozen=True)
class Rect:
    """舱室横截面内的矩形占位，坐标原点为舱室左下角，单位米。"""

    x_m: float
    y_m: float
    width_m: float
    height_m: float

    def __post_init__(self) -> None:
        if self.width_m <= 0 or self.height_m <= 0:
            raise ValueError("矩形尺寸必须大于零")

    @property
    def x2(self) -> float:
        return self.x_m + self.width_m

    @property
    def y2(self) -> float:
        return self.y_m + self.height_m

    def intersects(self, other: "Rect") -> bool:
        return (
            self.x_m < other.x2
            and other.x_m < self.x2
            and self.y_m < other.y2
            and other.y_m < self.y2
        )

    def gap_to(self, other: "Rect") -> float:
        """两个矩形之间的最小净距，投影重叠方向的分量按 0 计。"""
        gap_x = max(0.0, self.x_m - other.x2, other.x_m - self.x2)
        gap_y = max(0.0, self.y_m - other.y2, other.y_m - self.y2)
        return (gap_x**2 + gap_y**2) ** 0.5


@dataclass(frozen=True)
class ChainageInterval:
    """沿管廊走向的连续里程区间，采用半开区间 [start_m, end_m)。"""

    start_m: float
    end_m: float

    def __post_init__(self) -> None:
        if self.end_m <= self.start_m:
            raise ValueError("里程区间终点必须大于起点")

    def overlaps(self, other: "ChainageInterval") -> bool:
        return self.start_m < other.end_m and other.start_m < self.end_m


@dataclass(frozen=True)
class TimeWindow:
    """作业时间窗，半开区间 [start, end)。"""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.start, datetime) or not isinstance(self.end, datetime):
            raise ValueError("时间窗必须是 datetime")
        if self.end <= self.start:
            raise ValueError("结束时间必须晚于开始时间")

    @classmethod
    def of(cls, start: Union[datetime, str], end: Union[datetime, str]) -> "TimeWindow":
        return cls(parse_dt(start), parse_dt(end))

    def overlaps(self, other: "TimeWindow") -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class Compartment:
    """舱室及其横截面净空：内宽内高、永久设施占位、必须保持畅通的检修/逃生通道。"""

    compartment_id: str
    name: str
    width_m: float
    height_m: float
    fixtures: Tuple[Rect, ...] = field(default_factory=tuple)
    safety_passage: Optional[Rect] = None

    def __post_init__(self) -> None:
        if self.width_m <= 0 or self.height_m <= 0:
            raise ValueError("舱室截面尺寸必须大于零")
        for f in self.fixtures:
            if not self.contains(f):
                raise ValueError(f"永久设施 {f} 超出舱室 {self.compartment_id} 截面")
        if self.safety_passage is not None and not self.contains(self.safety_passage):
            raise ValueError("安全通道超出舱室截面")

    def contains(self, rect: Rect) -> bool:
        return (
            rect.x_m >= 0
            and rect.y_m >= 0
            and rect.x2 <= self.width_m
            and rect.y2 <= self.height_m
        )


@dataclass(frozen=True)
class Segment:
    """舱室内的连续区段（里程分段）。"""

    segment_id: str
    compartment_id: str
    chainage: ChainageInterval


@dataclass(frozen=True)
class WorkEnvelope:
    """单个占位包络：在某舱室某区段的某段里程上，占据横截面内的一个矩形。

    跨区段预约通过同一申请携带多个 WorkEnvelope 表达。
    """

    envelope_id: str
    compartment_id: str
    segment_id: str
    chainage: ChainageInterval
    footprint: Rect
    utility: UtilityKind

    def __post_init__(self) -> None:
        if not isinstance(self.utility, UtilityKind):
            raise ValueError("utility 必须是 UtilityKind")


@dataclass(frozen=True)
class Proposal:
    """一次施工作业方案提交。"""

    request_id: str
    contractor: str
    window: TimeWindow
    envelopes: Tuple[WorkEnvelope, ...]
    depends_on: Tuple[str, ...] = field(default_factory=tuple)
    priority: Optional[int] = None  # 数值越小越优先；缺省按专业默认值

    def __post_init__(self) -> None:
        if not self.envelopes:
            raise ValueError("方案至少包含一个占位包络")
        ids = [e.envelope_id for e in self.envelopes]
        if len(set(ids)) != len(ids):
            raise ValueError("包络编号不能重复")

    @property
    def utilities(self) -> set:
        return {e.utility for e in self.envelopes}

    @property
    def primary_utility(self) -> UtilityKind:
        return next(iter(self.utilities))

    def effective_priority(self) -> int:
        if self.priority is not None:
            return self.priority
        return min(DEFAULT_RECOVERY_PRIORITY[u] for u in self.utilities)


# ---------------------------------------------------------------------------
# 以下为脚手架阶段保留的简化契约，保持与既有测试/脚本兼容。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TunnelSection:
    section_id: str
    start_m: float
    end_m: float

    def __post_init__(self) -> None:
        if self.end_m <= self.start_m:
            raise ValueError("区段终点必须晚于起点")


@dataclass(frozen=True)
class OccupancyEnvelope:
    request_id: str
    section_id: str
    width_m: float
    height_m: float

    def __post_init__(self) -> None:
        if self.width_m <= 0 or self.height_m <= 0:
            raise ValueError("占位尺寸必须大于零")
