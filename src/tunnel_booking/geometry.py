"""一维里程区间与二维断面矩形的空间关系计算。"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Interval:
    """半开区间 [start, end)，用于里程与时间的重叠判断。"""

    start: float
    end: float

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("区间终点必须大于起点")

    @property
    def length(self) -> float:
        return self.end - self.start

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and other.start < self.end

    def intersection(self, other: "Interval") -> "Interval | None":
        s, e = max(self.start, other.start), min(self.end, other.end)
        return Interval(s, e) if e > s else None


@dataclass(frozen=True)
class Rect:
    """横截面内的矩形占位，原点在舱室左下角，单位米。"""

    x_m: float
    y_m: float
    width_m: float
    height_m: float

    def __post_init__(self) -> None:
        if self.width_m <= 0 or self.height_m <= 0:
            raise ValueError("矩形尺寸必须大于零")

    @property
    def right(self) -> float:
        return self.x_m + self.width_m

    @property
    def top(self) -> float:
        return self.y_m + self.height_m

    @property
    def area(self) -> float:
        return self.width_m * self.height_m

    def overlaps(self, other: "Rect") -> bool:
        return (
            self.x_m < other.right
            and other.x_m < self.right
            and self.y_m < other.top
            and other.y_m < self.top
        )

    def distance_to(self, other: "Rect") -> float:
        """两矩形的最小欧氏距离；相交时为 0。"""
        dx = max(other.x_m - self.right, self.x_m - other.right, 0.0)
        dy = max(other.y_m - self.top, self.y_m - other.top, 0.0)
        return math.hypot(dx, dy)

    def fits_within(self, width_m: float, height_m: float) -> bool:
        return (
            self.x_m >= 0
            and self.y_m >= 0
            and self.right <= width_m
            and self.top <= height_m
        )


def format_chainage(m: float) -> str:
    """里程格式化为 K0+150.0 样式，便于阅读冲突原因。"""
    km = int(m // 1000)
    return f"K{km}+{m - km * 1000:05.1f}"
