"""管廊区段和占位包络的基础契约。

TunnelSection 描述一段连续区段：里程范围 + 舱室横截面（净宽净高、既有设施分区）。
OccupancyEnvelope 描述某条占位需求落在单个区段内的包络切片。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .geometry import Rect


@dataclass(frozen=True)
class Zone:
    """舱内既有设施分区（如既有电力电缆、通信桥架），对作业占位提出净空要求。"""

    zone_id: str
    name: str
    utility: str
    rect: Rect
    required_clearance_m: float = 0.5
    fixed: bool = True

    def to_dict(self) -> dict:
        return {
            "zone_id": self.zone_id,
            "name": self.name,
            "utility": self.utility,
            "rect": {
                "x_m": self.rect.x_m,
                "y_m": self.rect.y_m,
                "width_m": self.rect.width_m,
                "height_m": self.rect.height_m,
            },
            "required_clearance_m": self.required_clearance_m,
            "fixed": self.fixed,
        }

    @staticmethod
    def from_dict(d: dict) -> "Zone":
        r = d["rect"]
        return Zone(
            zone_id=d["zone_id"],
            name=d["name"],
            utility=d["utility"],
            rect=Rect(r["x_m"], r["y_m"], r["width_m"], r["height_m"]),
            required_clearance_m=d.get("required_clearance_m", 0.5),
            fixed=d.get("fixed", True),
        )


@dataclass(frozen=True)
class TunnelSection:
    """连续区段：start_m/end_m 为里程，width/height 为断面净宽净高。"""

    section_id: str
    start_m: float
    end_m: float
    width_m: float = 3.0
    height_m: float = 2.5
    min_clearance_m: float = 0.5
    min_separation_m: float = 0.3
    zones: tuple = ()
    emergency_min_level: int = 1

    def __post_init__(self) -> None:
        if self.end_m <= self.start_m:
            raise ValueError("区段终点必须晚于起点")
        if self.width_m <= 0 or self.height_m <= 0:
            raise ValueError("断面净宽净高必须大于零")
        if self.min_clearance_m < 0 or self.min_separation_m < 0:
            raise ValueError("净空与隔离距离不能为负")

    @property
    def length_m(self) -> float:
        return self.end_m - self.start_m

    def to_dict(self) -> dict:
        return {
            "section_id": self.section_id,
            "start_m": self.start_m,
            "end_m": self.end_m,
            "width_m": self.width_m,
            "height_m": self.height_m,
            "min_clearance_m": self.min_clearance_m,
            "min_separation_m": self.min_separation_m,
            "zones": [z.to_dict() for z in self.zones],
            "emergency_min_level": self.emergency_min_level,
        }

    @staticmethod
    def from_dict(d: dict) -> "TunnelSection":
        return TunnelSection(
            section_id=d["section_id"],
            start_m=d["start_m"],
            end_m=d["end_m"],
            width_m=d.get("width_m", 3.0),
            height_m=d.get("height_m", 2.5),
            min_clearance_m=d.get("min_clearance_m", 0.5),
            min_separation_m=d.get("min_separation_m", 0.3),
            zones=tuple(Zone.from_dict(z) for z in d.get("zones", ())),
            emergency_min_level=d.get("emergency_min_level", 1),
        )


@dataclass(frozen=True)
class OccupancyEnvelope:
    """占位包络：某条需求在单个区段内的断面占位与里程范围。

    chainage_start_m/chainage_end_m 为 None 时表示覆盖整个区段。
    """

    request_id: str
    section_id: str
    width_m: float
    height_m: float
    x_m: float = 0.0
    y_m: float = 0.0
    chainage_start_m: float | None = None
    chainage_end_m: float | None = None
    utility: str = "GENERAL"
    process: str = "GENERAL"

    def __post_init__(self) -> None:
        if self.width_m <= 0 or self.height_m <= 0:
            raise ValueError("占位尺寸必须大于零")

    @property
    def rect(self) -> Rect:
        return Rect(self.x_m, self.y_m, self.width_m, self.height_m)

    def chainage(self, section: TunnelSection) -> tuple:
        """解析后的里程范围；未指定时取区段全段。"""
        start = self.chainage_start_m if self.chainage_start_m is not None else section.start_m
        end = self.chainage_end_m if self.chainage_end_m is not None else section.end_m
        return start, end

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "section_id": self.section_id,
            "width_m": self.width_m,
            "height_m": self.height_m,
            "x_m": self.x_m,
            "y_m": self.y_m,
            "chainage_start_m": self.chainage_start_m,
            "chainage_end_m": self.chainage_end_m,
            "utility": self.utility,
            "process": self.process,
        }

    @staticmethod
    def from_dict(d: dict) -> "OccupancyEnvelope":
        return OccupancyEnvelope(
            request_id=d["request_id"],
            section_id=d["section_id"],
            width_m=d["width_m"],
            height_m=d["height_m"],
            x_m=d.get("x_m", 0.0),
            y_m=d.get("y_m", 0.0),
            chainage_start_m=d.get("chainage_start_m"),
            chainage_end_m=d.get("chainage_end_m"),
            utility=d.get("utility", "GENERAL"),
            process=d.get("process", "GENERAL"),
        )
