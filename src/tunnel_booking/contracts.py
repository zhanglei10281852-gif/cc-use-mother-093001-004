"""管廊区段和占位包络的基础契约。"""
from dataclasses import dataclass


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
