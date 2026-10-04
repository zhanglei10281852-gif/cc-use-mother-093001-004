"""运行期领域模型：占位需求、方案版本、作业许可与状态常量。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .geometry import Rect

# ---- 方案状态 ----
PLAN_SUBMITTED = "SUBMITTED"            # 已提交，待技术复核
PLAN_TECH_REJECTED = "TECH_REJECTED"    # 技术复核退回
PLAN_TECH_APPROVED = "TECH_APPROVED"    # 技术复核通过，待运营批准
PLAN_APPROVED = "APPROVED"              # 运营批准（已生成许可）
PLAN_SUPERSEDED = "SUPERSEDED"          # 被新版本取代
PLAN_CANCELLED = "CANCELLED"

# ---- 许可状态 ----
PERMIT_APPROVED = "APPROVED"                  # 已批准，未进场
PERMIT_ACTIVE = "ACTIVE"                      # 进场作业中
PERMIT_SUSPENDED = "SUSPENDED"                # 被抢修暂停
PERMIT_ACCEPTANCE = "ACCEPTANCE_PENDING"      # 已撤场，待验收
PERMIT_COMPLETED = "COMPLETED"                # 验收完成，占位释放
PERMIT_EXPIRED = "EXPIRED"                    # 审批过期未进场
PERMIT_CANCELLED = "CANCELLED"

# 仍处于占位状态的许可：撤场验收完成前不得释放占位
OCCUPYING_STATUSES = (
    PERMIT_APPROVED,
    PERMIT_ACTIVE,
    PERMIT_SUSPENDED,
    PERMIT_ACCEPTANCE,
)

PLAN_NORMAL = "NORMAL"
PLAN_EMERGENCY = "EMERGENCY"

PRIORITY_EMERGENCY = 0
PRIORITY_NORMAL = 100


def parse_dt(value) -> datetime:
    """解析 ISO8601 时间，统一为 UTC  aware datetime。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class OccupancyRequest:
    """方案中的一条占位需求；里程可跨多个连续区段。"""

    request_id: str
    chainage_start_m: float
    chainage_end_m: float
    rect: Rect
    utility: str
    process: str
    time_start: datetime
    time_end: datetime
    crew_size: int = 1

    def __post_init__(self) -> None:
        if self.chainage_end_m <= self.chainage_start_m:
            raise ValueError("占位终点里程必须大于起点")
        if self.time_end <= self.time_start:
            raise ValueError("占位结束时间必须晚于开始时间")

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "chainage_start_m": self.chainage_start_m,
            "chainage_end_m": self.chainage_end_m,
            "rect": {
                "x_m": self.rect.x_m,
                "y_m": self.rect.y_m,
                "width_m": self.rect.width_m,
                "height_m": self.rect.height_m,
            },
            "utility": self.utility,
            "process": self.process,
            "time_start": fmt_dt(self.time_start),
            "time_end": fmt_dt(self.time_end),
            "crew_size": self.crew_size,
        }

    @staticmethod
    def from_dict(d: dict) -> "OccupancyRequest":
        r = d["rect"]
        return OccupancyRequest(
            request_id=d["request_id"],
            chainage_start_m=float(d["chainage_start_m"]),
            chainage_end_m=float(d["chainage_end_m"]),
            rect=Rect(r["x_m"], r["y_m"], r["width_m"], r["height_m"]),
            utility=d.get("utility", "GENERAL"),
            process=d.get("process", "GENERAL"),
            time_start=parse_dt(d["time_start"]),
            time_end=parse_dt(d["time_end"]),
            crew_size=int(d.get("crew_size", 1)),
        )
